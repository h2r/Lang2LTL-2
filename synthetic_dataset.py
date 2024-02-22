import os
import argparse
import logging
from collections import defaultdict
import itertools
import random
import re

from utils import load_from_file, save_to_file


def split_true_lmk_grounds(lmks_fpath, loc, sp_fpath, res_fpath):
    """
    Split ``true_lmk_grounds.json`` into two files contains referring expressions per landmark
    and grounded spatial predications per spatial relation for each location.
    """
    lmk_grounds = load_from_file(lmks_fpath)[loc]
    sp_grounds = defaultdict(list)
    res = defaultdict(lambda: defaultdict(list))
    for lmk, grounds in lmk_grounds.items():
        for ground in grounds:
            if "*" in ground:  # unique referring expression can identify landmark without anchor
                res[lmk]["proper_names"].append(ground["*"])
                sp_grounds["None"].append(ground["*"])
            elif "@" in ground:  # ambiguous referring expression if used without anchor
                res[lmk]["generic_names"].append(ground["@"])
            else:  # spatial predicate grounding
                rel = list(ground.keys())[0]
                sp_grounds[rel].append(ground[rel])
    save_to_file(sp_grounds, sp_fpath)
    save_to_file(res, res_fpath)


def generate_dataset(ltl_fpath, sp_fpath, res_fpath, utts_fpath, outs_fpath, nsamples):
    """
    Generate input utterances and ground truth results for each grounding module.
    """
    random.seed(0)

    lifted_data = load_from_file(ltl_fpath)
    sp_grounds_all = load_from_file(sp_fpath)
    res_all = load_from_file(res_fpath)

    ltl2data = defaultdict(set)
    for pattern_type, props, utt_lifted, ltl_lifted in lifted_data:
        ltl2data[ltl_lifted].add((pattern_type, props, utt_lifted))

    logging.info(f"# unique lifted LTL formulas: {len(ltl2data)}")
    for ltl, data in sorted(ltl2data.items(), key=lambda kv: len(kv[0])):
        logging.info(f"{ltl}: {len(data)}")
    logging.info(f"# unique spatial relations: {len(sp_grounds_all)}")
    logging.info(f"# unique landmarks: {len(res_all)}")

    utts = ""
    true_outs = []

    for ltl_lifted, ltl_data in ltl2data.items():  # every lifted LTL formula
        data_sampled = random.sample(sorted(ltl_data), nsamples)
        for data in data_sampled:  # every sampled lifted utterances
            pattern_type, props, utt_lifted = data
            props_full = eval(props)
            props = props_full[0] if len(set(props)) == 1 else props_full  # e.g., visit a at most twice, ['a', 'a']
            rels = random.sample(sorted(sp_grounds_all), len(props))

            srer_outs = {}
            reg_spg_outs = {}

            for rel in rels:  # every sampled spatial relations
                sp_grounds_sampled = random.sample(sp_grounds_all[rel], 1)[0]
                res_true = []

                if rel == "None":  # referring expression without spatial relation
                    sre = sp_grounds_sampled
                    res_true.append(sp_grounds_sampled)
                elif len(sp_grounds_sampled) == 1:  # sre with only an anchor
                    while "proper_names" not in res_all[sp_grounds_sampled[0]]:
                        sp_grounds_sampled = random.sample(sp_grounds_all[rel], 1)[0]
                    re_tar = random.sample(res_all[sp_grounds_sampled[0]]["proper_names"], 1)[0]
                    res_true.append(re_tar)
                    sre = f"{rel} {re_tar}"
                else:  # for sre with target and one or two anchors, both proper and generic names are valid
                    res_tar = list(itertools.chain.from_iterable(res_all[sp_grounds_sampled[0][0]].values()))
                    re_tar = random.sample(res_tar, 1)[0]  # target referring expression
                    res_true.append(re_tar)
                    res_anch1 = list(itertools.chain.from_iterable(res_all[sp_grounds_sampled[1][0]].values()))
                    re_anc1 = random.sample(res_anch1, 1)[0]  # anchor 1 referring expression
                    res_true.append(re_anc1)
                    if len(sp_grounds_sampled) == 2:
                        sre = f"{re_tar} {rel} {re_anc1}"
                    else:
                        res_anc2 = list(itertools.chain.from_iterable(res_all[sp_grounds_sampled[2][0]].values()))
                        re_anc2 = random.sample(res_anc2, 1)[0] # anchor 2 referring expression
                        res_true.append(re_anc2)
                        sre = f"{re_tar} {rel} {re_anc1} and {re_anc2}"

                srer_outs[sre] = {rel: res_true}
                reg_spg_outs[sre] = sp_grounds_sampled

            if not utt_lifted.startswith('.'):
                utt_ground = '.' + utt_lifted
            if not utt_ground.endswith('.'):
                utt_ground += '.'
            for prop, sre in zip(props, srer_outs.keys()):
                utt_ground = re.sub(rf"(\b)([{prop}])(\W)", rf'\1{sre}\3', utt_ground)
            utt_ground = utt_ground[1:-1]
            utts += f"{utt_ground}\n"

            true_outs.append({
                "lifted_utt": utt_lifted,
                "pattern_type": pattern_type,
                "props": props_full,
                "utt": utt_ground,
                "srer_outs": srer_outs,
                "reg_spg_outs": reg_spg_outs,
                "lt_out": ltl_lifted
            })

    save_to_file(utts, utts_fpath)
    save_to_file(true_outs, outs_fpath)
    logging.info(f"# data points: {len(true_outs)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", type=str, default="boston", choices=["blackstone", "boston", "auckland"], help="domain name.")
    parser.add_argument("--nsamples", type=int, default=10, help="numbe of samples per LTL formula.")
    args = parser.parse_args()

    data_dpath = os.path.join(os.path.expanduser("~"), "ground", "data")
    ltl_fpath = os.path.join(data_dpath, "symbolic_batch12_noperm.csv")
    sp_fpath = os.path.join(data_dpath, f"{args.location}_sp_grounds.json")
    res_fpath = os.path.join(data_dpath,f"{args.location}_res.json")
    utts_fpath = os.path.join(data_dpath, f"{args.location}_utts_n{args.nsamples}.txt")
    outs_fpath = os.path.join(data_dpath, f"{args.location}_true_results_n{args.nsamples}.json")

    logging.basicConfig(level=logging.INFO,
                        format='%(message)s',
                        handlers=[
                            logging.FileHandler(os.path.join(data_dpath, f"{args.location}_synthetic_dataset.log"), mode='w'),
                            logging.StreamHandler()
                        ]
    )
    logging.info(f"Generating dataset location: {args.location}\n***** Dataset Statisitcs\n")

    if not os.path.isfile(sp_fpath) or not os.path.isfile(res_fpath):
        lmks_fpath = os.path.join(data_dpath, "true_lmk_grounds.json")
        split_true_lmk_grounds(lmks_fpath, args.location, sp_fpath, res_fpath)

    if not os.path.isfile(utts_fpath):
        generate_dataset(ltl_fpath, sp_fpath, res_fpath, utts_fpath, outs_fpath, args.nsamples)
