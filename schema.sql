-- Пользователи
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  telegram_id BIGINT UNIQUE NOT NULL,
  username TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Аквариумы (по пользователю: несколько штук)
CREATE TABLE IF NOT EXISTS aquariums (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  volume_liters NUMERIC(6,2),
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Параметры воды (история замеров)
CREATE TABLE IF NOT EXISTS water_params (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
  ph NUMERIC(4,2),
  kh NUMERIC(4,2),
  gh NUMERIC(4,2),
  no2 NUMERIC(6,3),
  no3 NUMERIC(6,3),
  tan NUMERIC(6,3),   -- Total Ammonia (NH3+NH4), мг/л
  po4 NUMERIC(6,3),
  temp_c NUMERIC(5,2),
  frac_nh3 NUMERIC(6,5), -- доля NH3 (unionized)
  nh3_mgl NUMERIC(6,3),  -- NH3 mg/L (unionized)
  tested_at TIMESTAMPTZ DEFAULT now()
);

-- Рыбы и растения (состав аквариума)
CREATE TABLE IF NOT EXISTS fishes (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plants (
  id SERIAL PRIMARY KEY,
  aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Ускорим выборки
CREATE INDEX IF NOT EXISTS idx_aquariums_user ON aquariums(user_id);
CREATE INDEX IF NOT EXISTS idx_water_params_aq_time ON water_params(aquarium_id, tested_at DESC);
