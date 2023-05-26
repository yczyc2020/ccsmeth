import os
import argparse
import sys
import time
import numpy as np
from statsmodels import robust
import multiprocessing as mp
from multiprocessing import Queue
import gzip

import pysam
from tqdm import tqdm

from .utils.process_utils import display_args
from .utils.process_utils import codecv1_to_frame2
from .utils.process_utils import get_refloc_of_methysite_in_motif
from .utils.process_utils import get_motif_seqs
from .utils.process_utils import complement_seq
from .utils.process_utils import base2code_dna
from .utils.process_utils import compute_pct_identity
from .utils.process_utils import get_q2tloc_from_cigar
from .utils.process_utils import str2bool
from .utils.process_utils import index_bam_if_needed2

# from .utils.process_utils import run_cmd
# from .utils.process_utils import generate_samtools_index_cmd

from .utils.ref_reader import DNAReference

from .utils.process_utils import default_ref_loc

code2frames = codecv1_to_frame2()
queue_size_border = 1000
time_wait = 1


# check and read some inputs =============================================
def check_input_file(inputfile):
    if not (inputfile.endswith(".bam") or inputfile.endswith(".sam")):
        raise ValueError("--input/-i must be in bam/sam format!")
    inputpath = os.path.abspath(inputfile)
    return inputpath


def check_output_file(outputfile, inputfile):
    if outputfile is None:
        fname, fext = os.path.splitext(inputfile)
        output_path = fname + ".features.tsv"
    else:
        output_path = os.path.abspath(outputfile)
    return output_path


def _open_inputfile(inputfile, rmode, threads=1):
    if inputfile.endswith(".bam"):
        if rmode == "align":
            try:
                inputreads = pysam.AlignmentFile(inputfile, 'rb', threads=threads)
            except ValueError:
                sys.stderr.write("[WARN] The input file has no sequences defined - Please align "
                                 "the reads to genome reference first, or use '--mode denovo'\n")
                return None
        else:
            inputreads = pysam.AlignmentFile(inputfile, 'rb', check_sq=False, threads=threads)
    else:
        inputreads = pysam.AlignmentFile(inputfile, 'r', threads=threads)
    return inputreads


def _get_holes(holeidfile):
    holes = set()
    with open(holeidfile, "r") as rf:
        for line in rf:
            words = line.strip().split("\t")
            holeid = words[0]
            holes.add(holeid)
    sys.stderr.write("get {} holeids from {}\n".format(len(holes), holeidfile))
    return holes


# read bam/sam inputfile =============================================
def _get_necessary_items_of_a_alignedsegment(readitem):
    seq_name = readitem.query_name
    qalign_start = readitem.query_alignment_start
    qalign_end = readitem.query_alignment_end
    fwd_seq = readitem.get_forward_sequence()
    fwd_qual = readitem.get_forward_qualities()
    ref_name = readitem.reference_name
    ref_start = readitem.reference_start
    ref_end = readitem.reference_end
    cigar_tuples = readitem.cigartuples
    cigar_stats = readitem.get_cigar_stats()
    flag = readitem.flag
    mapq = readitem.mapping_quality
    is_unmapped = readitem.is_unmapped
    is_secondary = readitem.is_secondary
    is_duplicate = readitem.is_duplicate
    is_supplementary = readitem.is_supplementary
    is_reverse = readitem.is_reverse

    try:
        tag_fi = readitem.get_tag("fi")
        tag_ri = readitem.get_tag("ri")
        tag_fp = readitem.get_tag("fp")
        tag_rp = readitem.get_tag("rp")
    except KeyError:
        tag_fi = tag_ri = tag_fp = tag_rp = []
    try:
        tag_fn = readitem.get_tag("fn")
        tag_rn = readitem.get_tag("rn")
    except KeyError:
        tag_fn = tag_rn = 0
    return seq_name, qalign_start, qalign_end, fwd_seq, fwd_qual, ref_name, ref_start, ref_end, \
        cigar_tuples, cigar_stats, flag, mapq, is_unmapped, is_secondary, is_duplicate, is_supplementary, \
        is_reverse, tag_fi, tag_ri, tag_fp, tag_rp, tag_fn, tag_rn


def worker_read_split_holebatches_to_queue(inputfile, holebatch_q, threads, args):
    sys.stderr.write("split_holebatches process-{} starts\n".format(os.getpid()))
    inputreads = _open_inputfile(inputfile, args.mode, threads=args.threads)
    if inputreads is None:
        holebatch_q.put("kill")
        return
    # TODO: check if input is generated by --by-strand/--hd-finder?
    if args.mode == "align":
        # totalnum = inputreads.count()
        totalnum = inputreads.count(until_eof=True)
    else:
        totalnum = inputreads.count(until_eof=True)
    inputreads.close()

    holebatches = []
    for i in np.arange(0, totalnum, args.holes_batch):
        # holebatches.append((i, (i + args.holes_batch)))
        holebatches.append(i)
    sys.stderr.write("split_holebatches process-{} generates {} "
                     "hole/read batches({})\n".format(os.getpid(), len(holebatches),
                                                      args.holes_batch))

    with tqdm(total=len(holebatches),
              desc="batch_reader") as pbar:
        inputreads = _open_inputfile(inputfile, args.mode, threads=threads)
        all_reads = inputreads.fetch(until_eof=True)
        count = 0
        count_batch = 0
        holebatchtmp = []
        for readitem in all_reads:
            readinfo = _get_necessary_items_of_a_alignedsegment(readitem)
            holebatchtmp.append(readinfo)
            count += 1
            if count % args.holes_batch == 0 or count == totalnum:
                holebatch_q.put(holebatchtmp)
                pbar.update(1)
                count_batch += 1
                holebatchtmp = []
                while holebatch_q.qsize() > queue_size_border:
                    time.sleep(time_wait)
        inputreads.close()
        if count_batch != len(holebatches):
            sys.stderr.write("[WARN]read {} batches while it should be {} batches!".format(count_batch,
                                                                                           len(holebatches)))
        if len(holebatchtmp) > 0:
            sys.stderr.write("[WARN]There are still holes/reads that do not belong any batches!")
    holebatch_q.put("kill")
    sys.stderr.write("split_holebatches process-{} finished\n".format(os.getpid()))


# extract features =============================================
def _normalize_signals(signals, normalize_method="zscore"):
    if normalize_method == 'zscore':
        sshift, sscale = np.mean(signals), np.std(signals)
    elif normalize_method == 'min-max':
        sshift, sscale = np.min(signals), np.max(signals) - np.min(signals)
    elif normalize_method == 'min-mean':
        sshift, sscale = np.min(signals), np.mean(signals)
    elif normalize_method == 'mad':
        sshift, sscale = np.median(signals), float(robust.scale.mad(signals))
    else:
        raise ValueError("")
    if sscale == 0.0:
        norm_signals = [0.] * len(signals)
    else:
        norm_signals = (signals - sshift) / sscale
    return np.around(norm_signals, decimals=6)


def _get_q2t_mapinfo(q2t_loc, q_seq, t_seq):
    assert len(q2t_loc) == len(q_seq) + 1
    q2t_map = np.full(len(q2t_loc), 0, dtype=np.int32)

    if q2t_loc[0] == -1:  # insertion 000/001
        q2t_map[0] = 1
    elif q_seq[0].upper() != t_seq[q2t_loc[0]].upper():  # mismatch 000/100
        q2t_map[0] = 4

    if len(q2t_loc) > 2:
        for idx in range(1, len(q2t_loc)-1):
            if q2t_loc[idx] == -1:  # insertion 000/001
                q2t_map[idx] = 1
            else:
                if q_seq[idx].upper() != t_seq[q2t_loc[idx]].upper():  # mismatch 000/100
                    q2t_map[idx] += 4
                if q2t_loc[idx-1] != -1 and q2t_loc[idx] != q2t_loc[idx-1] + 1:  # deletion 000/010
                    q2t_map[idx] += 2
    return q2t_map


def _get_fr_kmer_mapinfo(offset_idx, offset_revidx, num_bases, q_to_r_mapinfo):
    q_to_r_mapinfo_s = q_to_r_mapinfo[:-1]  # ori len of q_to_r_mapinfo = len(seq_seq) + 1

    if offset_idx - num_bases >= 0:
        offset_s = offset_idx - num_bases
        pad_l = 0
    else:
        offset_s = 0
        pad_l = num_bases - offset_idx
    if offset_idx + num_bases < len(q_to_r_mapinfo_s):
        offset_e = offset_idx + num_bases + 1
        pad_r = 0
    else:
        offset_e = len(q_to_r_mapinfo_s)
        pad_r = num_bases + 1 - (len(q_to_r_mapinfo_s) - offset_idx)
    fkmer_map = np.pad(q_to_r_mapinfo_s[offset_s:offset_e],
                       (pad_l, pad_r),
                       mode='constant', constant_values=1)

    if offset_revidx - num_bases >= 0:
        offset_s = offset_revidx - num_bases
        pad_l = 0
    else:
        offset_s = 0
        pad_l = num_bases - offset_revidx
    if offset_revidx + num_bases < len(q_to_r_mapinfo_s):
        offset_e = offset_revidx + num_bases + 1
        pad_r = 0
    else:
        offset_e = len(q_to_r_mapinfo_s)
        pad_r = num_bases + 1 - (len(q_to_r_mapinfo_s) - offset_revidx)
    rkmer_map = np.flip(np.pad(q_to_r_mapinfo_s[offset_s:offset_e],
                               (pad_l, pad_r),
                               mode='constant', constant_values=1))

    return fkmer_map, rkmer_map


def extract_features_from_double_strand_read(readinfo, motifs, holeids_e, holeids_ne, dnacontigs,
                                             args):
    seq_name, qalign_start, qalign_end, fwd_seq, fwd_qual, ref_name, ref_start, ref_end, \
        cigar_tuples, cigar_stats, flag, mapq, is_unmapped, is_secondary, is_duplicate, is_supplementary, \
        is_reverse, tag_fi, tag_ri, tag_fp, tag_rp, tag_fn, tag_rn = readinfo

    if holeids_e is not None and seq_name not in holeids_e:
        return []
    if holeids_ne is not None and seq_name in holeids_ne:
        return []
    if args.mode == "align":
        if is_unmapped or is_secondary or is_duplicate:
            if str2bool(args.loginfo):
                sys.stderr.write("[WARN]read-{} is unmapped/secondary/duplicate\n".format(seq_name))
            return []
        if args.no_supplementary and is_supplementary:
            if str2bool(args.loginfo):
                sys.stderr.write("[WARN]read-{} is supplementary\n".format(seq_name))
            return []
        if mapq < args.mapq:
            if str2bool(args.loginfo):
                sys.stderr.write("[WARN]read-{} has low mapQ({})\n".format(seq_name, mapq))
            return []
        identity = compute_pct_identity(np.array(cigar_stats[0]))
        if identity < args.identity:
            if str2bool(args.loginfo):
                sys.stderr.write("[WARN]read-{} has low map identity({})\n".format(seq_name, identity))
            return []

    # extract features
    seq_seq = fwd_seq
    seq_rc = complement_seq(seq_seq)
    seq_qual = np.array(fwd_qual, dtype=int) if len(fwd_qual) > 0 else np.full(len(seq_seq), 0, dtype=np.int32)
    if str2bool(args.loginfo):
        sys.stderr.write("[WARN]read-{} has no base quality\n".format(seq_name))
    seq_qual = _normalize_signals(seq_qual, args.norm)
    reverse = is_reverse

    # change seq_start/seq_end if is_reverse
    if reverse:
        seq_start = len(seq_seq) - qalign_end
        seq_end = len(seq_seq) - qalign_start
    else:
        seq_start = qalign_start
        seq_end = qalign_end

    q_to_r_poss = None
    q_to_r_mapinfo = None
    if args.mode == "align":
        strand_code = -1 if reverse else 1
        q_to_r_poss = get_q2tloc_from_cigar(cigar_tuples, strand_code, (seq_end - seq_start))
        if str2bool(args.is_mapfea):
            refseq = dnacontigs[ref_name][ref_start:ref_end]
            if reverse:
                refseq = complement_seq(refseq)
            q_to_r_mapinfo = _get_q2t_mapinfo(q_to_r_poss, seq_seq[seq_start:seq_end], refseq)

    ipdmean_fwd = np.array(tag_fi, dtype=int)
    # ipdmean_rev = np.flip(np.array(tag_ri, dtype=int))
    ipdmean_rev = np.array(tag_ri, dtype=int)  # no need to use np.filp to reverse
    pwmean_fwd = np.array(tag_fp, dtype=int)
    # pwmean_rev = np.flip(np.array(tag_rp, dtype=int))
    pwmean_rev = np.array(tag_rp, dtype=int)
    if len(ipdmean_fwd) != len(seq_seq) or len(pwmean_fwd) != len(seq_seq):
        if str2bool(args.loginfo):
            sys.stderr.write("[WARN]read-{} has no/uncomplated fwd ipd/pw values\n".format(seq_name))
        return []
    if len(ipdmean_rev) != len(seq_seq) or len(pwmean_rev) != len(seq_seq):
        if str2bool(args.loginfo):
            sys.stderr.write("[WARN]read-{} has no/uncomplated rev ipd/pw values\n".format(seq_name))
        return []
    if not args.no_decode:
        ipdmean_fwd = np.array([code2frames[val] for val in ipdmean_fwd])
        ipdmean_rev = np.array([code2frames[val] for val in ipdmean_rev])
        pwmean_fwd = np.array([code2frames[val] for val in pwmean_fwd])
        pwmean_rev = np.array([code2frames[val] for val in pwmean_rev])
    ipdmean_fwd = _normalize_signals(ipdmean_fwd, args.norm)
    ipdmean_rev = _normalize_signals(ipdmean_rev, args.norm)
    pwmean_fwd = _normalize_signals(pwmean_fwd, args.norm)
    pwmean_rev = _normalize_signals(pwmean_rev, args.norm)

    npass_fwd = tag_fn
    npass_rev = tag_rn

    # WARN: motifs needs to be symmetric seq, like CG/GATC
    motif_len = len(motifs[0])
    rev_offset_loc = (motif_len - 1 - args.mod_loc) - args.mod_loc
    tsite_locs = get_refloc_of_methysite_in_motif(seq_seq, set(motifs), args.mod_loc)
    num_bases = (args.seq_len - 1) // 2
    feature_list = []
    for loc in tsite_locs:
        rev_loc = loc + rev_offset_loc
        rev_loc_in_rev = len(seq_seq) - 1 - rev_loc
        if num_bases <= loc < len(seq_seq) - num_bases and num_bases <= rev_loc_in_rev < len(seq_seq) - num_bases:
            fkmer_seq = seq_seq[(loc - num_bases):(loc + num_bases + 1)]
            fkmer_im = ipdmean_fwd[(loc - num_bases):(loc + num_bases + 1)]
            fkmer_isd = "."
            fkmer_pm = pwmean_fwd[(loc - num_bases):(loc + num_bases + 1)]
            fkmer_psd = "."
            fkmer_qual = seq_qual[(loc - num_bases):(loc + num_bases + 1)]

            rkmer_seq = seq_rc[(rev_loc_in_rev - num_bases):(rev_loc_in_rev + num_bases + 1)]
            rkmer_im = ipdmean_rev[(rev_loc_in_rev - num_bases):(rev_loc_in_rev + num_bases + 1)]
            rkmer_isd = "."
            rkmer_pm = pwmean_rev[(rev_loc_in_rev - num_bases):(rev_loc_in_rev + num_bases + 1)]
            rkmer_psd = "."
            rkmer_qual = np.flip(seq_qual[(rev_loc - num_bases):(rev_loc + num_bases + 1)])

            if q_to_r_poss is not None:
                chrom = ref_name
                chrom_pos = default_ref_loc
                strand = "-" if reverse else "+"
                fkmer_map = "."
                rkmer_map = "."

                if seq_start <= loc < seq_end:
                    offset_idx = loc - seq_start
                    offset_revidx = rev_loc - seq_start
                    if q_to_r_poss[offset_idx] != -1:
                        if reverse:
                            chrom_pos = ref_end - 1 - q_to_r_poss[offset_idx]
                        else:
                            chrom_pos = q_to_r_poss[offset_idx] + ref_start

                    if str2bool(args.is_mapfea):
                        fkmer_map, rkmer_map = _get_fr_kmer_mapinfo(offset_idx, offset_revidx, num_bases,
                                                                    q_to_r_mapinfo)
                else:
                    if str2bool(args.skip_unmapped):  # skip soft clip region
                        continue
                    if str2bool(args.is_mapfea):
                        fkmer_map = np.full(args.seq_len, 1, dtype=np.int32)
                        rkmer_map = np.full(args.seq_len, 1, dtype=np.int32)
            else:
                chrom = "."
                chrom_pos = default_ref_loc
                strand = "."
                fkmer_map = "."
                rkmer_map = "."
            feature_list.append([chrom, chrom_pos, strand, seq_name, loc,
                                 fkmer_seq, npass_fwd, fkmer_im, fkmer_isd, fkmer_pm, fkmer_psd,
                                 fkmer_qual, fkmer_map,
                                 rkmer_seq, npass_rev, rkmer_im, rkmer_isd, rkmer_pm, rkmer_psd,
                                 rkmer_qual, rkmer_map,
                                 args.methy_label])
    return feature_list


def process_one_holebatch(holebatch, motifs, holeids_e, holeids_ne, dnacontigs, args):
    feature_list = []
    read_idx = 0
    total_num = 0
    failed_num = 0
    for readinfo in holebatch:
        features_one = extract_features_from_double_strand_read(readinfo,
                                                                motifs, holeids_e, holeids_ne,
                                                                dnacontigs,
                                                                args)
        if len(features_one) == 0:
            failed_num += 1
        else:
            feature_list += features_one
        total_num += 1
        read_idx += 1
    return feature_list, total_num, failed_num


def _features_to_str(features):
    """

    :param features: a tuple
    :return:
    """
    chrom, chrom_pos, strand, seq_name, loc, \
        fkmer_seq, npass_fwd, fkmer_im, fkmer_isd, fkmer_pm, fkmer_psd, \
        fkmer_qual, fkmer_map, \
        rkmer_seq, npass_rev, rkmer_im, rkmer_isd, rkmer_pm, rkmer_psd, \
        rkmer_qual, rkmer_map, \
        label = features

    fkmer_im_str = ",".join([str(x) for x in fkmer_im])
    fkmer_isd_str = ",".join([str(x) for x in fkmer_isd]) if type(fkmer_isd) is not str else "."
    fkmer_pm_str = ",".join([str(x) for x in fkmer_pm])
    fkmer_psd_str = ",".join([str(x) for x in fkmer_psd]) if type(fkmer_psd) is not str else "."
    fkmer_qual_str = ",".join([str(x) for x in fkmer_qual])
    fkmer_map_str = ",".join([str(x) for x in fkmer_map]) if type(fkmer_map) is not str else "."

    rkmer_im_str = ",".join([str(x) for x in rkmer_im])
    rkmer_isd_str = ",".join([str(x) for x in rkmer_isd]) if type(rkmer_isd) is not str else "."
    rkmer_pm_str = ",".join([str(x) for x in rkmer_pm])
    rkmer_psd_str = ",".join([str(x) for x in rkmer_psd]) if type(rkmer_psd) is not str else "."
    rkmer_qual_str = ",".join([str(x) for x in rkmer_qual])
    rkmer_map_str = ",".join([str(x) for x in rkmer_map]) if type(rkmer_map) is not str else "."

    return "\t".join([chrom, str(chrom_pos), strand, seq_name, str(loc),
                      fkmer_seq, str(npass_fwd), fkmer_im_str, fkmer_isd_str, fkmer_pm_str, fkmer_psd_str,
                      fkmer_qual_str, fkmer_map_str,
                      rkmer_seq, str(npass_rev), rkmer_im_str, rkmer_isd_str, rkmer_pm_str, rkmer_psd_str,
                      rkmer_qual_str, rkmer_map_str,
                      str(label)])


def _batch_feature_list2s(feature_list):
    sampleinfo = []  # contains: chrom, abs_loc, strand, holeid, loc

    fkmers = []
    fpasss = []
    fipdms = []
    fipdsds = []
    fpwms = []
    fpwsds = []
    fquals = []
    fmaps = []

    rkmers = []
    rpasss = []
    ripdms = []
    ripdsds = []
    rpwms = []
    rpwsds = []
    rquals = []
    rmaps = []

    labels = []
    for featureline in feature_list:
        chrom, abs_loc, strand, holeid, loc, \
            kmer_seq, kmer_pass, kmer_ipdm, kmer_ipds, kmer_pwm, kmer_pws, kmer_qual, kmer_map, \
            kmer_seq2, kmer_pass2, kmer_ipdm2, kmer_ipds2, kmer_pwm2, kmer_pws2, kmer_qual2, kmer_map2, \
            label = featureline

        sampleinfo.append("\t".join(list(map(str, [chrom, abs_loc, strand, holeid, loc]))))

        fkmers.append(np.array([base2code_dna[x] for x in kmer_seq]))
        fpasss.append(np.array([kmer_pass] * len(kmer_seq)))
        fipdms.append(np.array(kmer_ipdm, dtype=float))
        fipdsds.append(np.array(kmer_ipds, dtype=float) if type(kmer_ipds) is not str else 0)
        fpwms.append(np.array(kmer_pwm, dtype=float))
        fpwsds.append(np.array(kmer_pws, dtype=float) if type(kmer_pws) is not str else 0)
        fquals.append(np.array(kmer_qual, dtype=float))
        fmaps.append(np.array(kmer_map, dtype=float) if type(kmer_map) is not str else 0)

        rkmers.append(np.array([base2code_dna[x] for x in kmer_seq2]))
        rpasss.append(np.array([kmer_pass2] * len(kmer_seq2)))
        ripdms.append(np.array(kmer_ipdm2, dtype=float))
        ripdsds.append(np.array(kmer_ipds2, dtype=float) if type(kmer_ipds2) is not str else 0)
        rpwms.append(np.array(kmer_pwm2, dtype=float))
        rpwsds.append(np.array(kmer_pws2, dtype=float) if type(kmer_pws2) is not str else 0)
        rquals.append(np.array(kmer_qual2, dtype=float))
        rmaps.append(np.array(kmer_map2, dtype=float) if type(kmer_map2) is not str else 0)

        labels.append(label)
    return sampleinfo, fkmers, fpasss, fipdms, fipdsds, fpwms, fpwsds, fquals, fmaps, \
        rkmers, rpasss, ripdms, ripdsds, rpwms, rpwsds, rquals, rmaps, labels


def worker_extract_features_from_holebatches(holebatch_q, features_q,
                                             motifs, holeids_e, holeids_ne, dnacontigs, args,
                                             is_tostr=True, is_batchlize=False):
    assert not (is_tostr and is_batchlize)
    sys.stderr.write("extract_features process-{} starts\n".format(os.getpid()))
    cnt_holesbatch = 0
    total_num_batch, failed_num_batch = 0, 0
    while True:
        if holebatch_q.empty():
            time.sleep(time_wait)
            continue
        holebatch = holebatch_q.get()
        if holebatch == "kill":
            holebatch_q.put("kill")
            break
        # handle one holebatch
        feature_list, total_num, failed_num = process_one_holebatch(holebatch,
                                                                    motifs, holeids_e, holeids_ne,
                                                                    dnacontigs,
                                                                    args)
        total_num_batch += total_num
        failed_num_batch += failed_num
        if len(feature_list) > 0:
            features_batch = []
            if is_tostr:
                for feature in feature_list:
                    features_batch.append(_features_to_str(feature))
            else:
                features_batch = feature_list
            if not is_tostr and is_batchlize:  # if is_to_str, then ignore is_batchlize
                features_batch = _batch_feature_list2s(features_batch)

            features_q.put(features_batch)
            while features_q.qsize() > queue_size_border:
                time.sleep(time_wait)
        cnt_holesbatch += 1
    sys.stderr.write("extract_features process-{} ending, proceed {} "
                     "hole_batches({}): {} holes/reads in total, "
                     "{} skipped/failed.\n".format(os.getpid(),
                                                   cnt_holesbatch,
                                                   args.holes_batch,
                                                   total_num_batch,
                                                   failed_num_batch))


# write to file =============================================
def _write_featurestr_to_file(write_fp, featurestr_q, is_gzip):
    sys.stderr.write('write_process-{} starts\n'.format(os.getpid()))
    if is_gzip:
        if not write_fp.endswith(".gz"):
            write_fp += ".gz"
        wf = gzip.open(write_fp, "wt")
    else:
        wf = open(write_fp, 'w')
    while True:
        # during test, it's ok without the sleep(time_wait)
        if featurestr_q.empty():
            time.sleep(time_wait)
            continue
        features_str = featurestr_q.get()
        if features_str == "kill":
            wf.close()
            sys.stderr.write('write_process-{} finished\n'.format(os.getpid()))
            break
        for one_features_str in features_str:
            wf.write(one_features_str + "\n")
        wf.flush()


def extract_hifireads_features(args):
    sys.stderr.write("[extract_features_hifi]starts\n")
    start = time.time()

    inputpath = check_input_file(args.input)
    if not os.path.exists(inputpath):
        raise IOError("input file does not exist!")
    index_bam_if_needed2(inputpath, args.threads)

    outputpath = check_output_file(args.output, inputpath)

    if args.seq_len % 2 == 0:
        raise ValueError("--seq_len must be odd")

    dnacontigs = None
    if args.mode == "align":
        if args.ref is None:
            raise ValueError("--ref must be provided when using align mode!")
        reference = os.path.abspath(args.ref)
        if not os.path.exists(reference):
            raise IOError("reference(--ref) file does not exist!")
        dnacontigs = DNAReference(reference).getcontigs()

    holeids_e = None if args.holeids_e is None else _get_holes(args.holeids_e)
    holeids_ne = None if args.holeids_ne is None else _get_holes(args.holeids_ne)
    motifs = get_motif_seqs(args.motifs)

    holebatch_q = Queue()
    features_q = Queue()

    # holebatches = split_inputreads_by_holebatch(inputpath, args)
    p_split = mp.Process(target=worker_read_split_holebatches_to_queue,
                         args=(inputpath, holebatch_q, 2, args))
    p_split.daemon = True
    p_split.start()

    ps_extract = []
    nproc = args.threads
    if nproc <= 3:
        nproc = 1
    else:
        nproc -= 3  # 2 for reading, 1 for writing
    for _ in range(nproc):
        p = mp.Process(target=worker_extract_features_from_holebatches,
                       args=(holebatch_q, features_q, motifs, holeids_e, holeids_ne, dnacontigs, args,
                             True, False))
        p.daemon = True
        p.start()
        ps_extract.append(p)

    p_w = mp.Process(target=_write_featurestr_to_file, args=(outputpath, features_q, args.gzip))
    p_w.daemon = True
    p_w.start()

    while True:
        # print("killing _worker_extract process")
        running = any(p.is_alive() for p in ps_extract)
        if not running:
            break

    for p in ps_extract:
        p.join()
    p_split.join()

    features_q.put("kill")
    p_w.join()

    endtime = time.time()
    sys.stderr.write("[extract_features_hifi]costs {:.1f} seconds\n".format(endtime - start))


def main():
    parser = argparse.ArgumentParser()

    p_input = parser.add_argument_group("INPUT")
    p_input.add_argument("--input", "-i", type=str, required=True,
                         help="input file in bam/sam format, "
                              "can be unaligned hifi.bam/sam and aligned sorted hifi.bam/sam.")
    p_input.add_argument("--holeids_e", type=str, default=None, required=False,
                         help="file contains holeids/hifiids to be extracted, default None")
    p_input.add_argument("--holeids_ne", type=str, default=None, required=False,
                         help="file contains holeids/hifiids not to be extracted, default None")

    p_output = parser.add_argument_group("OUTPUT")
    p_output.add_argument("--output", "-o", type=str, required=False,
                          help="output file path to save the extracted features. "
                               "If not specified, use input_prefix.tsv as default.")
    p_output.add_argument("--gzip", action="store_true", default=False, required=False,
                          help="if compressing the output using gzip")

    p_extract = parser.add_argument_group("EXTRACTION")
    p_extract.add_argument("--mode", type=str, default="denovo", required=False,
                           choices=["denovo", "align"],
                           help="denovo mode: extract features from unaligned/aligned hifi.bam without "
                                "reference position info;\n"
                                "align mode: extract features from aligned hifi.bam with "
                                "reference position info. default: denovo")
    p_extract.add_argument("--seq_len", type=int, default=21, required=False,
                           help="len of kmer. default 21")
    p_extract.add_argument("--motifs", action="store", type=str,
                           required=False, default='CG',
                           help='motif seq to be extracted, default: CG. '
                                'can be multi motifs splited by comma '
                                '(no space allowed in the input str), '
                                'or use IUPAC alphabet, '
                                'the mod_loc of all motifs must be '
                                'the same')
    p_extract.add_argument("--mod_loc", action="store", type=int, required=False, default=0,
                           help='0-based location of the targeted base in the motif, default 0')
    p_extract.add_argument("--methy_label", action="store", type=int,
                           choices=[1, 0], required=False, default=1,
                           help="the label of the interested modified bases, this is for training."
                                " 0 or 1, default 1")
    p_extract.add_argument("--norm", action="store", type=str, choices=["zscore", "min-mean", "min-max", "mad"],
                           default="zscore", required=False,
                           help="method for normalizing ipd/pw in subread level. "
                                "zscore, min-mean, min-max or mad, default zscore")
    p_extract.add_argument("--no_decode", action="store_true", default=False, required=False,
                           help="not use CodecV1 to decode ipd/pw")
    # p_extract.add_argument("--path_to_samtools", type=str, default=None, required=False,
    #                        help="full path to the executable binary samtools file. "
    #                             "If not specified, it is assumed that samtools is in "
    #                             "the PATH.")
    p_extract.add_argument("--holes_batch", type=int, default=50, required=False,
                           help="number of holes/hifi-reads in an batch to get/put in queues, default 50")

    p_extract_ref = parser.add_argument_group("EXTRACTION ALIGN_MODE")
    p_extract_ref.add_argument("--ref", type=str, required=False,
                               help="path to genome reference to be aligned, in fasta/fa format.")
    p_extract_ref.add_argument("--mapq", type=int, default=1, required=False,
                               help="MAPping Quality cutoff for selecting alignment items, default 1")
    p_extract_ref.add_argument("--identity", type=float, default=0.0, required=False,
                               help="identity cutoff for selecting alignment items, [0.0, 1.0], default 0.0")
    p_extract_ref.add_argument("--no_supplementary", action="store_true", default=False, required=False,
                               help="not use supplementary alignment")
    p_extract_ref.add_argument("--is_mapfea", type=str, default="no", required=False,
                               help="if extract mapping features, yes or no, default no")
    p_extract_ref.add_argument("--skip_unmapped", type=str, default="yes", required=False,
                               help="if skipping unmapped sites in reads, yes or no, default yes")

    parser.add_argument("--threads", type=int, default=5, required=False,
                        help="number of threads, default 5")
    parser.add_argument("--loginfo", type=str, default="no", required=False,
                        help="if printing more info of feature extraction on reads. "
                             "yes or no, default no")

    args = parser.parse_args()

    display_args(args, True)
    extract_hifireads_features(args)


if __name__ == '__main__':
    main()
