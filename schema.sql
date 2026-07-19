-- ============================================================================
--  Deliverex Bot — схема базы данных (PostgreSQL)
--  Выполнить один раз на Postgres-инстансе Railway.
--  Бот применяет этот же скрипт сам при старте, так что запускать вручную
--  не обязательно — но можно, если хотите создать структуру заранее.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Участники чата
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    tg_id        BIGINT      PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    last_name    TEXT,
    is_trusted   BOOLEAN     NOT NULL DEFAULT FALSE,  -- иммунитет к антиспаму
    is_admin     BOOLEAN     NOT NULL DEFAULT FALSE,
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- когда вошёл в группу
    left_at      TIMESTAMPTZ,
    warns        INTEGER     NOT NULL DEFAULT 0,
    mutes        INTEGER     NOT NULL DEFAULT 0,
    banned       BOOLEAN     NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username  ON users (LOWER(username));
CREATE INDEX IF NOT EXISTS idx_users_joined_at ON users (joined_at DESC);

-- ---------------------------------------------------------------------------
-- Заявки (просчёт стоимости), которые бот пересылает менеджеру
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
    id             BIGSERIAL   PRIMARY KEY,
    user_tg_id     BIGINT      NOT NULL REFERENCES users (tg_id) ON DELETE CASCADE,
    username       TEXT,
    full_name      TEXT,
    chat_id        BIGINT,                              -- где оставлена заявка
    thread_id      BIGINT,                              -- ID темы (topic) супергруппы
    message_id     BIGINT,
    source         TEXT        NOT NULL DEFAULT 'group',-- group | dm | command
    raw_text       TEXT        NOT NULL,
    category       TEXT,                                -- 📂 категория товара
    weight_kg      NUMERIC(12, 3),                      -- ⚖️ вес
    volume_m3      NUMERIC(12, 3),                      -- 📐 объём
    status         TEXT        NOT NULL DEFAULT 'new',  -- new | forwarded | in_work | done | spam
    forwarded_at   TIMESTAMPTZ,
    manager_msg_id BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_leads_user    ON leads (user_tg_id);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status  ON leads (status);

-- ---------------------------------------------------------------------------
-- Нарушения / модерация
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warnings (
    id           BIGSERIAL   PRIMARY KEY,
    user_tg_id   BIGINT      NOT NULL,
    chat_id      BIGINT,
    reason       TEXT        NOT NULL,
    score        INTEGER     NOT NULL DEFAULT 0,
    message_text TEXT,
    action       TEXT        NOT NULL DEFAULT 'delete', -- delete | warn | mute | ban
    by_admin_id  BIGINT,                                -- NULL = автоматически ботом
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_warnings_user    ON warnings (user_tg_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_warnings_created ON warnings (created_at DESC);

-- ---------------------------------------------------------------------------
-- Правила антиспама (пополняются админом прямо из чата: /spam_add ...)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spam_rules (
    id          BIGSERIAL   PRIMARY KEY,
    pattern     TEXT        NOT NULL,
    kind        TEXT        NOT NULL DEFAULT 'word',  -- word | regex
    score       INTEGER     NOT NULL DEFAULT 3,
    note        TEXT,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_spam_rules_pattern UNIQUE (pattern)
);

CREATE INDEX IF NOT EXISTS idx_spam_rules_enabled ON spam_rules (enabled);

-- ---------------------------------------------------------------------------
-- Получатели заявок (менеджеры). chat_id заполняется, когда менеджер
-- напишет боту /start в личку — иначе Telegram не даст боту ему написать.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS managers (
    tg_id      BIGINT      PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    chat_id    BIGINT      NOT NULL,
    is_primary BOOLEAN     NOT NULL DEFAULT TRUE,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Настройки, которые можно менять на лету без редеплоя
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Базовый набор спам-правил (безопасно перезапускать: ON CONFLICT DO NOTHING)
-- ---------------------------------------------------------------------------
INSERT INTO spam_rules (pattern, kind, score, note) VALUES
    ('пишите в лс',          'word', 3, 'увод в личку'),
    ('пиши в лс',            'word', 3, 'увод в личку'),
    ('напишите в лс',        'word', 3, 'увод в личку'),
    ('пишите в личку',       'word', 3, 'увод в личку'),
    ('в личку',              'word', 2, 'увод в личку'),
    ('в лс',                 'word', 2, 'увод в личку'),
    ('в директ',             'word', 2, 'увод в личку'),
    ('жду в лс',             'word', 3, 'увод в личку'),
    ('подписывайтесь',       'word', 3, 'реклама канала'),
    ('подпишись',            'word', 3, 'реклама канала'),
    ('мой канал',            'word', 3, 'реклама канала'),
    ('наш канал',            'word', 3, 'реклама канала'),
    ('переходи по ссылке',   'word', 3, 'реклама'),
    ('предлагаю сотрудничество', 'word', 3, 'конкуренты'),
    ('ищу партнеров',        'word', 3, 'конкуренты'),
    ('дешевле чем у',        'word', 3, 'конкуренты'),
    ('возим дешевле',        'word', 3, 'конкуренты'),
    ('наша карго',           'word', 3, 'конкуренты'),
    ('карго компания',       'word', 2, 'конкуренты'),
    ('доставка из китая от', 'word', 2, 'конкуренты'),
    ('выкуп товара из китая','word', 2, 'конкуренты'),
    ('бесплатный курс',      'word', 3, 'инфобизнес'),
    ('заработок',            'word', 3, 'скам'),
    ('инвестиции',           'word', 3, 'скам'),
    ('крипт',                'word', 2, 'скам'),
    ('казино',               'word', 4, 'скам'),
    ('ставки на спорт',      'word', 4, 'скам'),
    ('раскрутка',            'word', 3, 'услуги'),
    ('накрутка',             'word', 3, 'услуги'),
    ('продвижение вашего',   'word', 3, 'услуги'),
    ('whatsapp',             'word', 2, 'увод на сторонний контакт'),
    ('вотсап',               'word', 2, 'увод на сторонний контакт'),
    ('ватсап',               'word', 2, 'увод на сторонний контакт'),
    ('вацап',                'word', 2, 'увод на сторонний контакт'),
    ('вичат',                'word', 2, 'увод на сторонний контакт'),
    ('wechat',               'word', 2, 'увод на сторонний контакт')
ON CONFLICT (pattern) DO NOTHING;
