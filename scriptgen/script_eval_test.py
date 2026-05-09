from script_search import ScriptMutator, load_json, token_mapping, config, ScriptWithMeta, to_script_import_name

categories = load_json('categories')
TB = [{"id":"_meta","author":"","name":""},"washerwoman","librarian","investigator","chef","empath","fortuneteller","undertaker","monk","ravenkeeper","virgin","slayer","soldier","mayor","butler","drunk","recluse","saint","poisoner","spy","scarletwoman","baron","imp"]
BMR = [{"id":"_meta","author":"","name":""},"grandmother","sailor","chambermaid","exorcist","innkeeper","gambler","gossip","courtier","professor","minstrel","tealady","pacifist","fool","tinker","moonchild","goon","lunatic","godfather","devilsadvocate","assassin","mastermind","zombuul","pukka","shabaloth","po"]
SAV = [{"id":"_meta","author":"","name":""},"clockmaker","dreamer","snakecharmer","mathematician","flowergirl","towncrier","oracle","savant","seamstress","philosopher","artist","juggler","sage","mutant","sweetheart","barber","klutz","eviltwin","witch","cerenovus","pithag","fanggu","vigormortis","nodashii","vortox"]
idx_to_token, idx_to_category = token_mapping(config, categories)
evaluator = ScriptMutator(config, idx_to_token, idx_to_category, [True] * len(idx_to_token))

def json_to_script(json_script) -> ScriptWithMeta:
    token_lower_to_idx = {to_script_import_name(token): idx for idx, token in enumerate(idx_to_token)}
    tokens = set()
    for token in json_script[1:]:
        tokens.add(token_lower_to_idx[token])
    return ScriptWithMeta(tokens, evaluator)


print("TB score:", json_to_script(TB).favor)
print("BMR score:", json_to_script(BMR).favor)
print("SAV score:", json_to_script(SAV).favor)
