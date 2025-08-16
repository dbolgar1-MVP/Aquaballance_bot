-- Создание пользователей, аквариумов, обитателей, измерений, настроек подмен
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_seen TIMESTAMPTZ DEFAULT now(),
    active_aquarium_id INTEGER
);

CREATE TABLE IF NOT EXISTS aquariums (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    volume_l NUMERIC(10,2),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aquariums_user ON aquariums(user_id);

CREATE TABLE IF NOT EXISTS aquarium_fish (
    id SERIAL PRIMARY KEY,
    aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
    species TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    added_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS aquarium_plants (
    id SERIAL PRIMARY KEY,
    aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
    species TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    added_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS water_settings (
    aquarium_id INTEGER PRIMARY KEY REFERENCES aquariums(id) ON DELETE CASCADE,
    change_volume_pct NUMERIC(5,2),
    period_days INTEGER
);

CREATE TABLE IF NOT EXISTS measurements (
    id SERIAL PRIMARY KEY,
    aquarium_id INTEGER NOT NULL REFERENCES aquariums(id) ON DELETE CASCADE,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ph NUMERIC(4,2),
    kh NUMERIC(5,2),
    gh NUMERIC(5,2),
    no2 NUMERIC(6,3),
    no3 NUMERIC(6,2),
    tan NUMERIC(6,3),
    nh3 NUMERIC(6,3),
    nh4 NUMERIC(6,3),
    po4 NUMERIC(6,2),
    temperature_c NUMERIC(5,2),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_meas_aq_time ON measurements(aquarium_id, measured_at DESC);
