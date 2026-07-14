"""Tests for assign_franchise(), ported from Tools/PlayStation/test_pipeline.py's TestAssignFranchise.

The legacy hardcoded FRANCHISE_MAP list is represented here as FranchiseRule rows (its real replacement,
the franchise_rules table), with priority set to the list's original index -- the list's literal order WAS
its precedence order, so this preserves every regression exactly.
"""

from __future__ import annotations

from curator.catalog.franchise_assigner import FranchiseRule, assign_franchise

_FRANCHISE_MAP = [
    (r"assassin.s creed", "Assassin's Creed"),
    (r"call of duty", "Call of Duty"),
    (r"resident evil", "Resident Evil"),
    (r"final fantasy (xv|vii|viii|ix|x|xi|xii|xiii|xiv|xvi|vii remake|vii rebirth)", "Final Fantasy"),
    (r"final fantasy", "Final Fantasy"),
    (r"crisis core.{0,5}final fantasy", "Final Fantasy"),
    (r"yakuza|like a dragon|judgment|lost judgment|ishin", "Like a Dragon / Yakuza"),
    (r"lego\b", "LEGO"),
    (r"\bfar cry\b", "Far Cry"),
    (r"need for speed", "Need for Speed"),
    (r"\bpersona [3-6]\b", "Persona"),
    (r"uncharted", "Uncharted"),
    (r"god of war", "God of War"),
    (r"spider.man", "Spider-Man"),
    (r"batman.{0,10}arkham", "Batman: Arkham"),
    (r"watch.?dogs", "Watch Dogs"),
    (r"ghost of tsushima", "Ghost of Tsushima"),
    (r"the witcher", "The Witcher"),
    (r"horizon (zero|forbidden|call)", "Horizon"),
    (r"borderlands", "Borderlands"),
    (r"\bdestiny\b", "Destiny"),
    (r"\bfifa\b|ea sports fc|\beas fc\b", "FIFA / EA Sports FC"),
    (r"\bnba 2k", "NBA 2K"),
    (r"\bmlb the show\b", "MLB The Show"),
    (r"\bfallout\b", "Fallout"),
    (r"elder scrolls", "The Elder Scrolls"),
    (r"\b(elden ring|dark souls|demon.s souls|sekiro|bloodborne)\b", "FromSoftware"),
    (r"crash bandicoot", "Crash Bandicoot"),
    (r"ratchet.{0,5}clank", "Ratchet & Clank"),
    (r"\bsackboy\b", "Sackboy"),
    (r"coffee talk", "Coffee Talk"),
    (r"jackbox", "Jackbox Party Pack"),
    (r"\bovercooked\b", "Overcooked"),
    (r"little nightmares", "Little Nightmares"),
    (r"mafia (i|ii|iii|definitive|trilogy)", "Mafia"),
    (r"\bgta |grand theft auto", "Grand Theft Auto"),
    (r"red dead", "Red Dead"),
    (r"dragon age", "Dragon Age"),
    (r"mass effect", "Mass Effect"),
    (r"mortal kombat", "Mortal Kombat"),
    (r"street fighter", "Street Fighter"),
    (r"\btekken\b", "Tekken"),
    (r"tales of\b", "Tales Of"),
    (r"atelier\b", "Atelier"),
    (r"persona\b", "Persona"),
    (r"dragon quest", "Dragon Quest"),
    (r"\bnioh\b", "Nioh"),
    (r"metro (exodus|2033|last light)", "Metro"),
    (r"wolfenstein", "Wolfenstein"),
    (r"\bdoom\b", "DOOM"),
    (r"alan wake", "Alan Wake"),
    (r"the quarry|man of medan|little hope|house of ashes|the devil in me|the outlast|outlast", "Horror"),
    (r"a plague tale", "A Plague Tale"),
    (r"star wars jedi", "Star Wars Jedi"),
    (r"monster hunter", "Monster Hunter"),
    (r"devil may cry", "Devil May Cry"),
    (r"diablo\b", "Diablo"),
    (r"overwatch\b", "Overwatch"),
    (r"sims?\b", "The Sims"),
    (r"nfs|need for speed", "Need for Speed"),
    (r"\banno \d{4}\b", "Anno"),
    (r"\bark: survival", "ARK"),
    (r"\bbattlefield\b", "Battlefield"),
    (r"darksiders", "Darksiders"),
    (r"dishonored", "Dishonored"),
    (r"earth defense force", "Earth Defense Force"),
    (r"five nights at freddy", "Five Nights at Freddy's"),
    (r"ghostrunner", "Ghostrunner"),
    (r"goat simulator", "Goat Simulator"),
    (r"granblue fantasy", "Granblue Fantasy"),
    (r"hello neighbor", "Hello Neighbor"),
    (r"\bhitman\b", "Hitman"),
    (r"hot wheels", "Hot Wheels"),
    (r"just cause", "Just Cause"),
    (r"killing floor", "Killing Floor"),
    (r"life is strange", "Life is Strange"),
    (r"sniper elite", "Sniper Elite"),
    (r"\bsonic\b", "Sonic"),
    (r"south park", "South Park"),
    (r"tom clancy", "Tom Clancy"),
    (r"tomb raider", "Tomb Raider"),
    (r"\btrials\b", "Trials"),
    (r"\btropico\b", "Tropico"),
    (r"two point\b", "Two Point"),
    (r"warhammer", "Warhammer"),
    (r"\bys [ivx]", "Ys"),
]

_RULES = [
    FranchiseRule(rule_id=str(i), pattern=pattern, franchise=franchise, priority=i)
    for i, (pattern, franchise) in enumerate(_FRANCHISE_MAP)
]


def _assign(name: str) -> str:
    return assign_franchise(name, _RULES)


def test_anno_1800_matches():
    assert _assign("Anno 1800 Console Edition") == "Anno"


def test_anno_mutationem_does_not_match():
    # Regression: \banno\b matched "ANNO" in "ANNO: Mutationem", incorrectly awarding +1 franchise pt.
    # Fixed to \banno \d{4}\b.
    assert _assign("ANNO: Mutationem") == ""


def test_assassins_creed():
    assert _assign("Assassin's Creed Odyssey") == "Assassin's Creed"


def test_coffee_talk_both_entries():
    assert _assign("Coffee Talk") == "Coffee Talk"
    assert _assign("Coffee Talk Episode 2: Hibiscus & Butterfly") == "Coffee Talk"


def test_fromsoft_titles():
    assert _assign("Elden Ring") == "FromSoftware"
    assert _assign("Demon's Souls") == "FromSoftware"
    assert _assign("Bloodborne") == "FromSoftware"
    assert _assign("Sekiro: Shadows Die Twice") == "FromSoftware"


def test_god_of_war():
    assert _assign("God of War Ragnarok") == "God of War"


def test_no_franchise_for_standalone():
    assert _assign("art of rally") == ""
    assert _assign("Returnal") == ""


def test_nba_2k_with_year_matches():
    # Regression: \bnba 2k\b failed for "NBA 2K16" -- digit after "2k" is a word char so \b didn't fire.
    # Fixed to \bnba 2k (no trailing \b).
    assert _assign("NBA 2K25") == "NBA 2K"
    assert _assign("NBA 2K16") == "NBA 2K"


def test_watch_dogs_underscore_matches():
    # Regression: "watch dogs" (space) didn't match "Watch_Dogs2" (underscore). Fixed to watch.?dogs.
    assert _assign("Watch_Dogs2") == "Watch Dogs"


def test_eas_fc_matches():
    # Regression: "ea sports fc" didn't match "EAS FC 24" (abbreviated form).
    assert _assign("EAS FC 24") == "FIFA / EA Sports FC"


def test_ark_franchise():
    assert _assign("ARK: Survival Ascended") == "ARK"
    assert _assign("ARK: SURVIVAL EVOLVED") == "ARK"


def test_battlefield_franchise():
    assert _assign("Battlefield 2042") == "Battlefield"
    assert _assign("Battlefield V") == "Battlefield"


def test_darksiders_franchise():
    assert _assign("Darksiders III") == "Darksiders"


def test_dishonored_franchise():
    assert _assign("Dishonored 2") == "Dishonored"
    assert _assign("Dishonored: Death of the Outsider") == "Dishonored"


def test_earth_defense_force_franchise():
    assert _assign("EARTH DEFENSE FORCE 6") == "Earth Defense Force"
    assert _assign("Earth Defense Force 4.1: The Shadow of New Despair") == "Earth Defense Force"


def test_five_nights_franchise():
    assert _assign("Five Nights at Freddy's: Security Breach") == "Five Nights at Freddy's"
    assert _assign("Five Nights at Freddy's: Help Wanted 2") == "Five Nights at Freddy's"


def test_ghostrunner_franchise():
    assert _assign("Ghostrunner") == "Ghostrunner"
    assert _assign("Ghostrunner 2") == "Ghostrunner"


def test_goat_simulator_franchise():
    assert _assign("Goat Simulator 3") == "Goat Simulator"


def test_granblue_franchise():
    assert _assign("Granblue Fantasy: Relink") == "Granblue Fantasy"
    assert _assign("Granblue Fantasy Versus: Rising") == "Granblue Fantasy"


def test_hello_neighbor_franchise():
    assert _assign("Hello Neighbor 2") == "Hello Neighbor"


def test_hitman_franchise():
    assert _assign("HITMAN") == "Hitman"
    assert _assign("Hitman 2 - Gold Edition") == "Hitman"


def test_hot_wheels_franchise():
    assert _assign("HOT WHEELS UNLEASHED 2 - Turbocharged PS4 & PS5") == "Hot Wheels"


def test_just_cause_franchise():
    assert _assign("Just Cause 4") == "Just Cause"


def test_killing_floor_franchise():
    assert _assign("Killing Floor 3") == "Killing Floor"


def test_life_is_strange_franchise():
    assert _assign("Life is Strange 2") == "Life is Strange"


def test_sniper_elite_franchise():
    assert _assign("Sniper Elite 5") == "Sniper Elite"


def test_sonic_franchise():
    assert _assign("SONIC X SHADOW GENERATIONS") == "Sonic"
    assert _assign("Sonic Colors: Ultimate") == "Sonic"
    assert _assign("Team Sonic Racing") == "Sonic"


def test_south_park_franchise():
    assert _assign("South Park: The Stick of Truth") == "South Park"
    assert _assign("South Park: The Fractured but Whole") == "South Park"


def test_tom_clancy_franchise():
    assert _assign("Tom Clancy's Rainbow Six Siege") == "Tom Clancy"
    assert _assign("Tom Clancy's The Division 2") == "Tom Clancy"
    assert _assign("TOM CLANCY'S GHOST RECON BREAKPOINT") == "Tom Clancy"


def test_tomb_raider_franchise():
    assert _assign("Shadow of the Tomb Raider") == "Tomb Raider"
    assert _assign("Tomb Raider I-III Remastered Starring Lara Croft") == "Tomb Raider"


def test_trials_franchise():
    assert _assign("Trials Rising") == "Trials"
    assert _assign("Trials of The Blood Dragon") == "Trials"


def test_tropico_franchise():
    assert _assign("Tropico 6") == "Tropico"


def test_two_point_franchise():
    assert _assign("Two Point Hospital") == "Two Point"
    assert _assign("Two Point Campus") == "Two Point"


def test_warhammer_franchise():
    assert _assign("Warhammer 40,000: Space Marine 2") == "Warhammer"
    assert _assign("WARHAMMER: VERMINTIDE 2") == "Warhammer"


def test_ys_franchise():
    assert _assign("Ys VIII: Lacrimosa of DANA") == "Ys"
    assert _assign("Ys IX: Monstrum Nox") == "Ys"


def test_rule_order_is_independent_of_input_list_order():
    shuffled = list(reversed(_RULES))
    assert assign_franchise("Elden Ring", shuffled) == "FromSoftware"
