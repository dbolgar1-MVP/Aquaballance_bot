-- Schema for Aquaballance_bot (MVP)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  last_name TEXT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aquariums (
  id SERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  volume_l REAL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS water_tests (
  id SERIAL PRIMARY KEY,
  aquarium_id INT REFERENCES aquariums(id) ON DELETE CASCADE,
  measured_at TIMESTAMP WITH TIME ZONE NOT NULL,
  ph REAL,
  kh REAL,
  gh REAL,
  no2 REAL,
  no3 REAL,
  total_ammonia_mg_l REAL,
  nh3_calculated_mg_l REAL,
  nh3_fraction REAL,
  po4 REAL,
  temp_c REAL,
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS species (
  id SERIAL PRIMARY KEY,
  name TEXT,
  type TEXT, -- fish/plant
  ph_min REAL, ph_max REAL,
  gh_min REAL, gh_max REAL,
  temp_min REAL, temp_max REAL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS aquarium_inhabitants (
  id SERIAL PRIMARY KEY,
  aquarium_id INT REFERENCES aquariums(id) ON DELETE CASCADE,
  species_id INT REFERENCES species(id) ON DELETE SET NULL,
  common_name TEXT,
  quantity INT DEFAULT 1,
  added_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
