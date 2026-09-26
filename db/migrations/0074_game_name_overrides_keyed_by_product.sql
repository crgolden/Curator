-- An override names one product, never every product under a concept: a game and its soundtrack app
-- share a concept, and an override keyed by the concept alone renamed both and folded them into one
-- games row. The key is the concept plus the product, and an entitlement carrying no product id takes no
-- override. A row keyed by a concept alone no longer says which product it names, so none survives.

ALTER TABLE game_name_overrides ADD COLUMN product_id TEXT;

DELETE FROM game_name_overrides WHERE product_id IS NULL;

ALTER TABLE game_name_overrides ALTER COLUMN product_id SET NOT NULL;

ALTER TABLE game_name_overrides DROP CONSTRAINT game_name_overrides_pkey;

ALTER TABLE game_name_overrides ADD CONSTRAINT game_name_overrides_pkey PRIMARY KEY (concept_id, product_id);
