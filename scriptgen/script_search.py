import collections
import configparser
import csv
import itertools
import random
import json
import os
import time
import pathlib
import statistics
import sys
import math
import multiprocessing
from typing import Literal
REPO_ROOT = pathlib.Path((__file__)).parent.parent

def load_json(fname):
    with open(REPO_ROOT / (fname + '.json'), 'r', encoding='utf-8', errors='replace') as f:
        return json.load(f)

CATEGORIES = ['townsfolk', 'outsider', 'minion', 'demon', 'traveller', 'fabled', 'loric']
USE_MP = os.getenv('DEBUG', 'false').lower() == 'false'
MATCHUPS_RAW = load_json('matchups')
JINXES_RAW = load_json('jinxes')
GROUPS_RAW = load_json('groups')
EXTRA_RAW = load_json('extra')
config = configparser.ConfigParser()
config.read(REPO_ROOT / 'scriptgen' / 'config.cfg')
CHAR_RANKINGS_RAW = load_json(config['WEIGHTS']['character_preference_json'])


def token_mapping(config, categories):
    categories_to_add = []
    for c in CATEGORIES:
        if config.getint('COUNTS', f'max_{c}') > 0:
            categories_to_add.append(c)

    idx_to_token = []
    idx_to_category = []
    for category_name in categories_to_add:
        for token in categories[category_name]:
            idx_to_token.append(token)
            idx_to_category.append(category_name)

    return idx_to_token, idx_to_category

def get_value(synergy_type: Literal['great','good','info','warning','conflict']):
    match synergy_type:
        case 'great':
            return 1.0
        case 'good':
            return 0.5
        case 'warning':
            return -0.5
        case 'conflict':
            return -1.5
        case 'info':
            return 0.1
        case _:
            raise ValueError()

def get_synergy_matrix(token_to_idx):
    N_TOKEN = len(token_to_idx)
    synergy = [[0.0] * N_TOKEN for _ in range(N_TOKEN)]

    def update_synergy(token1: int, token2: int, score_add: float):
        nonlocal synergy
        ind1, ind2 = token_to_idx[token1], token_to_idx[token2]
        synergy[ind1][ind2] += score_add
        synergy[ind1][ind2] += score_add

    for token_1, synergy_list in MATCHUPS_RAW.items():
        for token_2, synergies in synergy_list.items():
            if token_1 not in token_to_idx or token_2 not in token_to_idx:
                continue
            for synergy_type, synergy_str in synergies.items():
                update_synergy(token_1, token_2, get_value(synergy_type))

    # Normalize the synergy matrix to reduce the effect of unfarily prefering
    # one character by virtue of many interactions
    row_sums = [sum(row) for row in synergy]
    for i in range(N_TOKEN):
        for j in range(i, N_TOKEN):
            synergy[i][j] -=  (row_sums[i] + row_sums[j]) / N_TOKEN

    for jinx in JINXES_RAW:
        assert len(jinx['characters']) == 2
        token_1, token_2 = jinx['characters']
        if token_1 not in token_to_idx or token_2 not in token_to_idx:
            continue
        match jinx['interaction_type']:
            case 'Just For Fun':
                jinx_penalty = 1
            case 'Feelsbad':
                jinx_penalty = 5
            case _:
                jinx_penalty = 3
        update_synergy(token_1, token_2, -jinx_penalty  * config.getfloat('WEIGHTS', 'jinx_evasion_weight'))

    return synergy

def make_idx_to_preference(idx_to_token: list[str], config, synergy_matrix) -> dict[int, float]:
    
    idx_to_preference = []
    for idx, token in enumerate(idx_to_token):
        idx_to_preference.append(CHAR_RANKINGS_RAW[token] * config.getfloat('WEIGHTS', 'character_preference_weight_multiplier'))

    # Normalize the synergy matrix: classes with lots of synergies get
    # a lower score such that the expected value (EV) of total synergies is 0
    n_other_tokens_in_script = sum(config.getint('COUNTS', f'max_{c}') for c in CATEGORIES) - 1
    row_EVs = []
    for row in synergy_matrix:
        no_outlier_row = [r for r in row if abs(r) < 4]
        if len(no_outlier_row):
            row_EVs.append(statistics.fmean(no_outlier_row))
        else:
            row_EVs.append(0)
    for i in range(len(idx_to_preference)):
        idx_to_preference[i] -= (row_EVs[i] * n_other_tokens_in_script)
    return idx_to_preference

def parse_groups(token_to_idx, idx_to_category):
    evil_idx = [token_to_idx[token] for token in token_to_idx if idx_to_category[token_to_idx[token]] in ['minion', 'demon']]
    groups = []
    for g in GROUPS_RAW:
        token_idxs = set(token_to_idx[c] for c in g['characters'] if c in token_to_idx)
        evil_tokens = set(token_idx for token_idx in token_idxs if token_idx in evil_idx)
        group = {'recommended': g['recommended'], 'multiple': g['multiple'], 'not_only_good': g['not_only_good']}
        if g['recommended'] or g['multiple'] or g['not_only_good']:
            group['token_idxs'] = token_idxs
            # This group has an effect on the weight
            if g['not_only_good']:
                group['evil_tokens_idxs'] = evil_tokens
            groups.append(group)
        else:
            continue # Nothing to score in this group
    return groups

def parse_extra(token_to_idx):
    extra = []
    for e in EXTRA_RAW:
        try:
            extra.append({
                'token_idxs': set(token_to_idx[c] for c in e['characters']),
                'favor': sum([get_value(k) for k in e['interaction'].keys()])
            })
        except KeyError:
            continue # Group has a token that we aren't using
    return extra

class ScriptWithMeta:
    def __init__(self, tokens: set[int], mutator: "ScriptMutator"):
        self.tokens: set[int] = tokens
        self.cat_counts: dict[str, int] = self.compute_cat_counts(self.tokens, mutator.idx_to_category)
        self.favor: float = mutator.evaluate_script(self)

    @classmethod
    def compute_cat_counts(cls, tokens: set[int], idx_to_category: list[str]) -> dict[str, int]:
        counts = {c: 0 for c in CATEGORIES}
        for token in tokens:
            counts[idx_to_category[token]] += 1
        return counts

class ScriptMutator:
    def __init__(self, config, idx_to_token, idx_to_category, gene):
        self.config = config
        self.idx_to_token = [t for i, t in enumerate(idx_to_token) if gene[i]]
        self.idx_to_category = [c for i, c in enumerate(idx_to_category) if gene[i]]
        self.n_idx_per_category = collections.Counter(self.idx_to_category)
        self.token_to_idx = {token: i for i, token in enumerate(self.idx_to_token)}

        self.synergy_matrix = get_synergy_matrix(self.token_to_idx)

        self.idx_to_preference = make_idx_to_preference(self.idx_to_token, self.config, self.synergy_matrix)

        self.groups = parse_groups(self.token_to_idx, self.idx_to_category)

        self.extra = parse_extra(self.token_to_idx)


    def random_start_script(self) -> ScriptWithMeta:
        tokens = set()
        for c in CATEGORIES:
            valid_inds = [idx for idx, cat in enumerate(self.idx_to_category) if cat == c]
            if len(valid_inds) < self.config.getint('COUNTS', f'min_{c}'):
                raise ValueError(f"Not enough {c} tokens to satisfy min count")

            min_n_sample = max(0, self.config.getint('COUNTS', f'min_{c}'))
            max_n_sample = min(len(valid_inds), self.config.getint('COUNTS', f'max_{c}'))
            assert min_n_sample <= max_n_sample, f"Check config for {c}. Max is less than min."
            n_sample = random.randint(min_n_sample, max_n_sample)
            tokens.update(random.sample(valid_inds, n_sample))
        return ScriptWithMeta(tokens, self)

    def compute_extra_favor(self, tokens: list[str]) -> dict[str, int]:
        favor_bonus = 0
        for g in self.groups:
            group_success = True
            n_in_bag = len(g['token_idxs'] & tokens)
            if g['recommended'] and n_in_bag == 0:
                # Should be present
                group_success = False
            if g['multiple'] and n_in_bag == 1:
                # Should have multiple if present
                group_success = False
            if g['not_only_good'] and n_in_bag > 0:
                # Should not only be good characters if present
                n_evil_in_bag = len(g['evil_tokens_idxs'] & tokens)
                if n_evil_in_bag == 0:
                    group_success = False
            if not group_success:
                favor_bonus -= 5
        for e in self.extra:
            all_in_bag = len(e['token_idxs'] - tokens) == 0
            if all_in_bag:
                favor_bonus += e['favor']
        return favor_bonus

    def evaluate_script(self, script: ScriptWithMeta) -> float:
        favor = 0
        for token in script.tokens:
            favor += self.idx_to_preference[token]

        for c in CATEGORIES:
            favor -= 30 * max(0, 
                self.config.getint('COUNTS', f'min_{c}') - script.cat_counts[c], 
                script.cat_counts[c] - self.config.getint('COUNTS', f'max_{c}'))
        
        for token_1, token_2 in itertools.combinations(script.tokens, 2):
            favor += self.synergy_matrix[token_1][token_2]
        return favor + self.compute_extra_favor(script.tokens)

    def choose_random_new_token(self, tokens: list[int], category: str) -> int:
        valid_inds = []
        for idx, cat in enumerate(self.idx_to_category):
            if cat == category and idx not in tokens:
                valid_inds.append(idx)
        return random.choice(valid_inds)

    def mutate_script(self, script: ScriptWithMeta) -> ScriptWithMeta:
        rand_element = random.choice(list(script.tokens))
        delta_char_type = self.idx_to_category[rand_element]
        has_addable = script.cat_counts[delta_char_type] < self.n_idx_per_category[delta_char_type]
        mutations = ''
        if has_addable:
            mutations += 'c' * 5
            if self.config.getint('COUNTS', f'max_{delta_char_type}') > script.cat_counts[delta_char_type]:
                mutations += 'a'
        if self.config.getint('COUNTS', f'min_{delta_char_type}') < script.cat_counts[delta_char_type]:
            mutations += 'r'

        if len(mutations) == 0:
            return script

        mutation_type = random.choice(mutations)
        mutated_tokens = script.tokens.copy()

        match mutation_type:
            case 'a':
                # Add a token
                new_token = self.choose_random_new_token(script.tokens, delta_char_type)
                mutated_tokens.add(new_token)
                return ScriptWithMeta(mutated_tokens, self)
            case 'r':
                mutated_tokens.remove(rand_element)
                # Remove a token
                return ScriptWithMeta(mutated_tokens, self)
            case 'c':
                # Replace a token
                mutated_tokens.remove(rand_element)
                mutated_tokens.add(self.choose_random_new_token(script.tokens, delta_char_type))
                return ScriptWithMeta(mutated_tokens, self)
            case _: raise NotImplementedError()

    def improve_individual(self) -> tuple[float, set[int]]:
        iterations = 2000
        initial_temp = 10.0
        final_temp = 0.1
        try:
            current_script = self.random_start_script()
        except ValueError:
            return -1000, []
        current_score = self.evaluate_script(current_script)
        
        for i in range(iterations):
            temperature = initial_temp * ((final_temp / initial_temp) ** (i / iterations))
            
            mutated_script = self.mutate_script(current_script)
            mutated_score = self.evaluate_script(mutated_script)
            
            delta_score = mutated_score - current_score
            
            if delta_score > 0:
                accept = True
            else:
                accept = random.random() < math.exp(delta_score / temperature)
            
            if accept:
                current_script = mutated_script
                current_score = mutated_score
    
        return current_score, current_script.tokens

def gene_merge(gene_1, gene_2, dropout):
    new_gene = []
    for g1, g2 in zip(gene_1, gene_2):
        if g1 & g2:
            new_gene.append(True)
        elif ~g1 & ~g2:
            new_gene.append(False)
        else:
            new_gene.append(random.random() < dropout)

    return new_gene


def create_next_generation(top_performers: list[tuple[float, set[int], list[bool]]], 
                          population_size: int, dropout: float) -> list[list[bool]]:
    mutation_rate = 0.01
    new_population = []

    # Create children through crossover and mutation
    while len(new_population) < population_size:
        parent1 = random.choice(top_performers)[2]
        parent2 = random.choice(top_performers)[2]
        child_gene = gene_merge(parent1, parent2, dropout)
        
        # Apply mutations
        for i in range(len(child_gene)):
            if random.random() < mutation_rate:
                child_gene[i] = not child_gene[i]
        
        new_population.append(child_gene)
    
    return new_population

def to_script_import_name(name: str) -> str:
    return name.lower().replace(" ", "").replace(" ", "").replace("_", "").replace('-', '').replace("'", '')

def export_json(best_tokens: list[int], idx_to_token: list[str]) -> dict:
    out_file = pathlib.Path(__file__).parent / 'generated_script.json'
    with open(out_file, 'w+') as f:
        json.dump([{"id":"_meta","author":"scriptgen","name":""}] + [to_script_import_name(idx_to_token[idx]) for idx in best_tokens], f)
    print(f'Saved config to {out_file}')

def evaluate_gene_worker(config, idx_to_token, idx_to_category, gene: list[bool]) -> tuple[float, set[str], list[bool]]:
    mutator = ScriptMutator(config, idx_to_token, idx_to_category, gene)
    score, tokens = mutator.improve_individual()
    # Convert indices to actual token names (from filtered list)
    token_names = {mutator.idx_to_token[idx] for idx in tokens}
    return (score, token_names, gene)

def main():
    categories = load_json('categories')
    idx_to_token, idx_to_category = token_mapping(config, categories)

    population_size = 60
    generations = 10
    dropout = 0.05
    num_workers = multiprocessing.cpu_count()

    # Initialize population
    population = [[random.random() > dropout for _ in range(len(idx_to_token))] for _ in range(population_size)]
    best_overall_score = float('-inf')
    best_overall_tokens = None
    
    for generation in range(generations):
        # Evaluate population in parallel
        with multiprocessing.Pool(num_workers) as pool:
            population_scores = pool.starmap(
                evaluate_gene_worker,
                [(config, idx_to_token, idx_to_category, gene) for gene in population]
            )
            
        # Track overall best
        for score, tokens, _ in population_scores:
            if score > best_overall_score:
                best_overall_score = score
                best_overall_tokens = tokens
        
        population_scores.sort(key=lambda x: x[0], reverse=True)
        top_performers = population_scores[:10]

        # Create next generation
        population = create_next_generation(top_performers, population_size, dropout)
        
        print(f"Generation {generation + 1}/{generations}, Best score: {top_performers[0][0]:.2f}")
        
    print(f"Best score: {best_overall_score:.2f}")
    print(f"Script: {sorted(best_overall_tokens)}")
    # Convert token names back to indices for export
    token_indices = [idx_to_token.index(token) for token in best_overall_tokens]
    export_json(token_indices, idx_to_token)

if __name__ == "__main__":
    main()



