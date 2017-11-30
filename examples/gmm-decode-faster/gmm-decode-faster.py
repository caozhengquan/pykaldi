#!/usr/bin/env python
from __future__ import print_function, division

import logging
import sys
import time

from kaldi.decoder import FasterDecoderOptions, FasterDecoder
from kaldi.fstext import (SymbolTable, FstHeader, FstReadOptions, StdArc,
                          StdVectorFst, StdConstFst,
                          LatticeVectorFst, CompactLatticeVectorFst)
from kaldi.fstext.utils import (get_linear_symbol_sequence_from_lattice_fst,
                                acoustic_lattice_scale, scale_lattice,
                                convert_lattice_to_compact_lattice)
from kaldi.gmm.am import AmDiagGmm, DecodableAmDiagGmmScaled
from kaldi.hmm import TransitionModel
from kaldi.util.io import xopen
from kaldi.util.options import ParseOptions
from kaldi.util.table import (IntVectorWriter, SequentialMatrixReader,
                              CompactLatticeWriter)

def read_network(rxfilename):
    with xopen(rxfilename) as ki:
        if not ki.stream()._good():
            raise IOError("Could not open decoding-graph FST {}"
                          .format(rxfilename))
        hdr = FstHeader()
        if not hdr.read(ki.stream(), "<unknown>"):
            raise IOError("Reading FST: error reading FST header.")
        if hdr.arc_type() != StdArc.type():
            raise ValueError("FST with arc type {} not supported"
                             .format(hdr.arc_type()))
        ropts = FstReadOptions("<unspecified>", hdr)
        if hdr.fst_type() == "vector":
            decode_fst = StdVectorFst.read_from_stream(ki.stream(), ropts)
        elif hdr.fst_type() == "const":
            decode_fst = StdConstFst.read_from_stream(ki.stream(), ropts)
        else:
            raise ValueError("Reading FST: unsupported FST type: {}"
                             .format(hdr.fst_type()))
        if not decode_fst:
            raise IOError("Error reading FST (after reading header).")
        return decode_fst

def gmm_decode_faster(opts, decoder_opts, model_rxfilename, fst_rxfilename,
                      feature_rspecifier, words_wspecifier,
                      alignment_wspecifier, lattice_wspecifier):
    trans_model = TransitionModel()
    am_gmm = AmDiagGmm()
    with xopen(model_rxfilename) as ki:
        trans_model.read(ki.stream(), ki.binary)
        am_gmm.read(ki.stream(), ki.binary)

    words_writer = IntVectorWriter(words_wspecifier)
    alignment_writer = IntVectorWriter(alignment_wspecifier)
    clat_writer = CompactLatticeWriter(lattice_wspecifier)

    word_syms = None
    if opts.word_symbol_table != "":
        word_syms = SymbolTable.read_text(opts.word_symbol_table)
        if not word_syms:
            raise RuntimeError("Could not read symbol table from file {}"
                               .format(opts.word_symbol_table))

    feature_reader = SequentialMatrixReader(feature_rspecifier)

    decode_fst = read_network(fst_rxfilename)

    tot_like = 0.0
    frame_count = 0
    num_success, num_fail = 0, 0
    decoder = FasterDecoder(decode_fst, decoder_opts)

    start = time.time()
    for key, features in feature_reader:
        if features.num_rows == 0:
            logging.warning("Zero-length utterance: {}".format(key))
            num_fail += 1
            continue
        gmm_decodable = DecodableAmDiagGmmScaled(am_gmm, trans_model, features,
                                                 opts.acoustic_scale)
        decoder.decode(gmm_decodable)
        decoded = LatticeVectorFst()

        if ((opts.allow_partial or decoder.reached_final())
            and decoder.get_best_path(decoded)):
            if not decoder.reached_final():
                logging.warning("Decoder did not reach end-state, outputting "
                                "partial traceback since --allow-partial=true")
            num_success += 1
            frame_count += features.num_rows

            ret = get_linear_symbol_sequence_from_lattice_fst(decoded)
            alignment, words, weight = ret

            words_writer[key] = words
            if alignment_writer.is_open():
                alignment_writer[key] = alignment

            if lattice_wspecifier != "":
                if opts.acoustic_scale != 0.0:
                    scale = acoustic_lattice_scale(1.0 / opts.acoustic_scale)
                    scale_lattice(scale, decoded)
                clat = CompactLatticeVectorFst()
                convert_lattice_to_compact_lattice(decoded, clat, true)
                clat_writer[key] = clat

            if word_syms:
                print(key, end=' ', file=sys.stderr)
                for idx in words:
                    sym = word_syms.find_symbol(idx)
                    if sym == "":
                        raise RuntimeError("Word-id {} not in symbol table."
                                           .format(idx))
                    print(sym, end=' ', file=sys.stderr)
                print(file=sys.stderr)
            like = - (weight.value1 + weight.value2);
            tot_like += like
            logging.info("Log-like per frame for utterance {} is {} over {} "
                         "frames.".format(key, like / features.num_rows,
                                          features.num_rows))
            logging.debug("Cost for utterance {} is {} + {}"
                          .format(key, weight.value1, weight.value2))
        else:
            num_fail += 1
            logging.warning("Did not successfully decode utterance {}, len = {}"
                            .format(key, features.num_rows))

    elapsed = time.time() - start
    logging.info("Time taken [excluding initialization] {}s: real-time factor "
                 "assuming 100 frames/sec is {}"
                 .format(elapsed, elapsed * 100 / frame_count))
    logging.info("Done {} utterances, failed for {}"
                 .format(num_success, num_fail))
    logging.info("Overall log-likelihood per frame is {} over {} frames."
                 .format(tot_like / frame_count, frame_count))

    feature_reader.close()
    words_writer.close()
    if alignment_writer.is_open():
        alignment_writer.close()
    if clat_writer.is_open():
        clat_writer.close()

    return True if num_success != 0 else False

if __name__ == '__main__':
    # Configure log messages to look like Kaldi messages
    from kaldi import __version__
    logging.addLevelName(20, 'LOG')
    logging.basicConfig(format='%(levelname)s (%(module)s[{}]:%(funcName)s():'
                               '%(filename)s:%(lineno)s) %(message)s'
                               .format(__version__), level=logging.INFO)

    usage = """Decode features using GMM-based model.

    Usage:  gmm-decode-faster.py [options] model-in fst-in features-rspecifier
                words-wspecifier [alignments-wspecifier [lattice-wspecifier]]

    Note: lattices, if output, will just be linear sequences;
          use gmm-latgen-faster if you want "real" lattices.
    """
    po = ParseOptions(usage)
    decoder_opts = FasterDecoderOptions()
    decoder_opts.register(po, True)
    po.register_float("acoustic-scale", 0.1,
                      "Scaling factor for acoustic likelihoods")
    po.register_bool("allow-partial", True,
                     "Produce output even when final state was not reached")
    po.register_str("word-symbol-table", "",
                    "Symbol table for words [for debug output]");
    opts = po.parse_args()

    if po.num_args() < 4 or po.num_args() > 6:
        po.print_usage()
        sys.exit()

    model_rxfilename = po.get_arg(1)
    fst_rxfilename = po.get_arg(2)
    feature_rspecifier = po.get_arg(3)
    words_wspecifier = po.get_arg(4)
    alignment_wspecifier = po.get_opt_arg(5)
    lattice_wspecifier = po.get_opt_arg(6)

    gmm_decode_faster(opts, decoder_opts, model_rxfilename, fst_rxfilename,
                      feature_rspecifier, words_wspecifier,
                      alignment_wspecifier, lattice_wspecifier)