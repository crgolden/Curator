-- A PSN concept is not a product. A game and its soundtrack app share one concept and are two products
-- that install side by side, so canonicalization keeps each as its own games row and a concept links to
-- every product under it. game_name_overrides and global_exclusions referenced the single-column key; an
-- override or an exclusion on a concept now applies to each of its products and no longer needs a link
-- row to exist first. Deploy only after the Functions writer stops relying on ON CONFLICT (concept_id).

ALTER TABLE game_name_overrides DROP CONSTRAINT game_name_overrides_concept_id_fkey;

ALTER TABLE global_exclusions DROP CONSTRAINT global_exclusions_concept_id_fkey;

ALTER TABLE game_concepts DROP CONSTRAINT game_concepts_pkey;

ALTER TABLE game_concepts ADD CONSTRAINT game_concepts_pkey PRIMARY KEY (concept_id, game_id);
