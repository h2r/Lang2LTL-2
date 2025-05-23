import os
import json
import numpy as np
from tqdm import tqdm
from sklearn.metrics.pairwise import cosine_similarity

from openai_models import get_embed, translate
from utils import deserialize_props_str, load_from_file, save_to_file


def retriever(query, embeds_fpath, raw_data, topk):
    nprops_query = len(deserialize_props_str(query[1]))
    query = query[:1]

    # Select lifted commands and formulas with same nprops as query command
    # not work with SRER output for "go to a at most five times"
    # data = []
    # for ltl_type, props, utt, ltl in raw_data:
    #     nprops = len(deserialize_props_str(props))
    #     entry = [ltl_type, props, utt, ltl]
    #     if nprops == nprops_query and entry not in data:
    #         data.append(entry)
    # print(f"{len(data)} templates matched query nprops")
    data = raw_data

    # Embed lifted commands then save or load from cache
    embeds = []
    utt2embed = load_from_file(embeds_fpath) if os.path.isfile(embeds_fpath) else {}

    embeds_updated = False
    for idx, (_, _, utt, _) in enumerate(data):
        # print(f"{idx}/{len(data)}. getting embedding:\n{utt}")
        if utt in utt2embed:
            embed = utt2embed[utt]
        else:
            embed = get_embed(utt)  # embedding
            utt2embed[utt] = embed
            embeds_updated = True
            print(f"added new prompt embedding:\n{utt}")
        embeds.append(embed)
    if embeds_updated:
        save_to_file(utt2embed, embeds_fpath)
    embeds = np.array(embeds)

    # Retrieve prompt in-context examples
    embeds_updated = False
    query_str = json.dumps(query)
    if query_str in utt2embed:
        embed_query = utt2embed[query_str]
    else:
        embed_query = get_embed(query)
        utt2embed[query_str] = embed_query
        embeds_updated = True
        print(f"added new query embedding:\n{utt}")
    if embeds_updated:
        save_to_file(utt2embed, embeds_fpath)

    query_scores = cosine_similarity(np.array(embed_query).reshape(1, -1), embeds)[0]
    data_sorted = sorted(zip(query_scores, data), reverse=True)

    prompt_examples = []
    for score, (ltl_type, props, utt, ltl) in data_sorted[:topk]:
        # print(score)
        prompt_examples.append(f"Command: \"{utt}\"\nLTL formula: \"{ltl}\"")
        # print(f"Command: \"{utt}\"\nLTL formula: \"{ltl}\"\n")

    return prompt_examples


def lifted_translate(query, embeds_fpath, raw_data, topk):
    prompt_examples = retriever(query, embeds_fpath, raw_data, topk)

    # breakpoint()

    lifted_ltl, num_tokens = translate(query[0], prompt_examples)
    return lifted_ltl, num_tokens


def lt(data_dpath, srer_out_fname, raw_data, topk):
    lt_outs = []
    srer_outs = load_from_file(os.path.join(data_dpath, srer_out_fname))

    for srer_out in srer_outs:
        query = [srer_out['lifted_utt'], json.dumps(list(srer_out["lifted_symbol_map"].keys()))]
        lifted_ltl, num_tokens = lifted_translate(query, raw_data, topk)

        # print(f"query: {query}\n{lifted_ltl}\n")

        # breakpoint()

    save_to_file(lt_outs, os.path.join(data_dpath, srer_out_fname.replace("srer", "lt")))

    return lifted_ltl, num_tokens


def run_exp_lt_rag(spg_out_fpath, lt_out_fpath, data_dpath, ltl_fpath, topk):
    if not os.path.isfile(lt_out_fpath):
        raw_data = load_from_file(ltl_fpath)
        spg_outs = load_from_file(spg_out_fpath)
        embeds_fpath = os.path.join(data_dpath, f"data_embeds.pkl")

        tot_tokens = 0

        for spg_out in tqdm(spg_outs, desc="Running lifted translation (LT) module (method='rag')"):
            query = [spg_out['lifted_utt'], json.dumps(list(spg_out["props"]))]
            lifted_ltl, num_tokens = lifted_translate(query, embeds_fpath, raw_data, topk)
            tot_tokens += num_tokens
            # print(f"query: {query}\n{lifted_ltl}\n")
            spg_out["lifted_ltl"] = lifted_ltl

        print(f'\nAVG. TOKEN SIZE:\t{tot_tokens / len(spg_outs)}')

        save_to_file(spg_outs, lt_out_fpath)


if __name__ == "__main__":
    data_dpath = os.path.join(os.path.expanduser("~"), "ground", "data")
    data_fpath = os.path.join(data_dpath, "symbolic_batch12_noperm.csv")
    raw_data = load_from_file(data_fpath)

    srer_out_fname = "srer_outs_blackstone.json"
    lt(data_dpath, srer_out_fname, raw_data, topk=50)

    # query = ["go to a at most five times", "['a', 'a', 'a', 'a', 'a']"]
    # lifted_translate(query, raw_data, topk=50)
