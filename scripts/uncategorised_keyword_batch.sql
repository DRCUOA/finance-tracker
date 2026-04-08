-- One-off batch: keywords + high-confidence recategorisation for user a6beaf68-88fd-4e8e-8dfc-6919a631456a
-- Run: psql ... -f scripts/uncategorised_keyword_batch.sql

BEGIN;

-- Debit Interest
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('9d5ccb53-ca00-4dc0-8f46-303e00fb235b'::uuid, 'loan interest'),
  ('9d5ccb53-ca00-4dc0-8f46-303e00fb235b'::uuid, 'loan drawdown')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Petrol (NZ chains missing from migrated keywords)
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('9a256b35-ecb4-4078-aca0-e7de4cdf336d'::uuid, 'bp connect'),
  ('9a256b35-ecb4-4078-aca0-e7de4cdf336d'::uuid, 'z lincoln'),
  ('9a256b35-ecb4-4078-aca0-e7de4cdf336d'::uuid, 'shell'),
  ('9a256b35-ecb4-4078-aca0-e7de4cdf336d'::uuid, 'caltex')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Groceries
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('96da2343-f152-4d8c-ab44-002f270b4a20'::uuid, 'wonkybox'),
  ('96da2343-f152-4d8c-ab44-002f270b4a20'::uuid, 'farro fresh'),
  ('96da2343-f152-4d8c-ab44-002f270b4a20'::uuid, 'piha store'),
  ('96da2343-f152-4d8c-ab44-002f270b4a20'::uuid, 'supa fruit mart')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Restaurants / fast food (statement text uses many spellings)
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'gourmet gannet'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'bun mee'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'mcdonald'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'starbucks'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'muffin break'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'dominos'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'kfc'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'nandos'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'burgerfuel'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'coffee club'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'esquires'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'the hangar'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'bistro box'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'common ground'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'siamese doll'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'girlz coffee'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'sweat shop brew'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'duck island'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'merchant pub'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'ajisen ramen'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'baha betty'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'westcity bakery'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'foxtrot parlour'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'whanga eats'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'toasted rosedale'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'stanby coffee'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'something social'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'dear friend'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'ring brothers'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'buns n rolls'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'smz*ronnies cafe'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'ripe deli'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'athenia in roma'),
  ('8e7909a1-04be-4a56-af67-df581135a9f0'::uuid, 'shamiana')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Cafe & Coffee (pods / boutique coffee)
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('c2bbbf6d-794a-4bc1-9c31-45e2e4e177d1'::uuid, 'nespresso'),
  ('c2bbbf6d-794a-4bc1-9c31-45e2e4e177d1'::uuid, 'bean grinding'),
  ('c2bbbf6d-794a-4bc1-9c31-45e2e4e177d1'::uuid, 'presshouse coffee')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Coding / domains / SaaS
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('66477a8f-71ae-4165-b152-a624625c42ce'::uuid, 'metaname'),
  ('66477a8f-71ae-4165-b152-a624625c42ce'::uuid, 'zoho')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Subscriptions (software)
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('bdb83244-bf28-4719-9131-b770f13a8bb8'::uuid, 'bitdefender'),
  ('bdb83244-bf28-4719-9131-b770f13a8bb8'::uuid, 'combitdefender')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- NZTA / vehicle charges
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('3a91b0d4-9499-4ed5-841c-8ea3a5a8baad'::uuid, 'nz transport agency')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Parking
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('d269261f-16d7-4e66-b0a1-aea56f6222d9'::uuid, 'civic car park'),
  ('d269261f-16d7-4e66-b0a1-aea56f6222d9'::uuid, 'parkiwi'),
  ('d269261f-16d7-4e66-b0a1-aea56f6222d9'::uuid, 'aucklandtransportpark'),
  ('d269261f-16d7-4e66-b0a1-aea56f6222d9'::uuid, 'at infringements')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Healthcare
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('48873008-f280-4f38-b97d-01c3656c1a47'::uuid, 'chemist warehouse'),
  ('48873008-f280-4f38-b97d-01c3656c1a47'::uuid, 'waitemata district hea')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Pet
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('6914b22e-d1dc-4fb5-897f-326777cd2c4a'::uuid, 'animates'),
  ('6914b22e-d1dc-4fb5-897f-326777cd2c4a'::uuid, 'forrest hill vet'),
  ('6914b22e-d1dc-4fb5-897f-326777cd2c4a'::uuid, 'hollard insu'),
  ('6914b22e-d1dc-4fb5-897f-326777cd2c4a'::uuid, 'petinsurance')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Gym
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('304d52ab-1cc7-4472-9753-cde1873e99ae'::uuid, 'ezi*health fitness'),
  ('304d52ab-1cc7-4472-9753-cde1873e99ae'::uuid, 'golden yogi')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Cosmetics / apparel retail
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('869089c3-7229-43e7-bac3-00357671d01d'::uuid, 'mecca brands'),
  ('869089c3-7229-43e7-bac3-00357671d01d'::uuid, 'rich lingerie'),
  ('869089c3-7229-43e7-bac3-00357671d01d'::uuid, 'jay jays'),
  ('869089c3-7229-43e7-bac3-00357671d01d'::uuid, 'stirling sports'),
  ('869089c3-7229-43e7-bac3-00357671d01d'::uuid, 'benefit ')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Books / stationery
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('d83714f2-8422-44bd-8312-5680b0d53e27'::uuid, 'whitcoulls'),
  ('d83714f2-8422-44bd-8312-5680b0d53e27'::uuid, 'warehouse stationery'),
  ('d83714f2-8422-44bd-8312-5680b0d53e27'::uuid, 'blackwells'),
  ('d83714f2-8422-44bd-8312-5680b0d53e27'::uuid, 'browns bay paper power'),
  ('d83714f2-8422-44bd-8312-5680b0d53e27'::uuid, 'paper moon')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Homewares / bed bath
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('869089c3-7229-43e7-bac3-00357671d01d'::uuid, 'bed bath n table'),
  ('2f32e325-7a5e-4d93-9bd3-bc6cb2c0f362'::uuid, 'matakana home')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Streaming / tickets
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('1a1663aa-158f-481c-a68d-946bbbdc3abc'::uuid, 'neon auckland'),
  ('8187f956-d3ab-467c-8b81-123e8a6a9523'::uuid, 'ticketmaster'),
  ('8187f956-d3ab-467c-8b81-123e8a6a9523'::uuid, 'humanitix'),
  ('8187f956-d3ab-467c-8b81-123e8a6a9523'::uuid, 'q theatre')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Travel / insurance / trips
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('d8061f73-ae25-4e62-916c-3138795bff70'::uuid, '1cover'),
  ('d8061f73-ae25-4e62-916c-3138795bff70'::uuid, 'kawau cruises'),
  ('d8061f73-ae25-4e62-916c-3138795bff70'::uuid, 'waitakere resort')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Gambling / casino
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('7e9cfbf5-4849-443b-af84-3373b2debe7a'::uuid, 'skycity depot')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

-- Takeaways (liquor can overlap with groceries — user may recategorise)
INSERT INTO category_keywords (id, category_id, keyword, hit_count)
SELECT gen_random_uuid(), cid, kw, 0
FROM (VALUES
  ('021fba7a-7090-407d-b4bc-bb59e0bdcb2e'::uuid, 'super liquor'),
  ('021fba7a-7090-407d-b4bc-bb59e0bdcb2e'::uuid, 'constellation liquor'),
  ('021fba7a-7090-407d-b4bc-bb59e0bdcb2e'::uuid, 'bottle o ')
) AS v(cid, kw)
WHERE NOT EXISTS (SELECT 1 FROM category_keywords ck WHERE ck.category_id = v.cid AND ck.keyword = v.kw);

COMMIT;
