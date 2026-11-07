-- Seller profile, Listing and Photo (data-model.md; tasks.md T036)..

CREATE TABLE seller_profile (
    account_sub  uuid PRIMARY KEY,
    display_name text        NOT NULL,
    sold_count   integer     NOT NULL DEFAULT 0 CHECK (sold_count >= 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE listing (
    id           uuid PRIMARY KEY,
    seller_sub   uuid        NOT NULL REFERENCES seller_profile (account_sub),
    title        text        NOT NULL CHECK (char_length(title) BETWEEN 3 AND 80),
    brand        text        NOT NULL CHECK (char_length(brand) BETWEEN 1 AND 40),
    category     text        NOT NULL CHECK (category IN ('tops', 'bottoms', 'dresses', 'outerwear', 'shoes', 'accessories')),
    size         text        NOT NULL CHECK (char_length(size) BETWEEN 1 AND 10),
    condition    text        NOT NULL CHECK (condition IN ('new_with_tags', 'like_new', 'good', 'fair')),
    price_cents  integer     NOT NULL CHECK (price_cents BETWEEN 100 AND 1000000),
    description  text        NOT NULL DEFAULT '' CHECK (char_length(description) <= 2000),
    status       text        NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'available', 'reserved', 'sold', 'deleted')),
    version      integer     NOT NULL DEFAULT 0,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(brand, '') || ' ' || coalesce(description, ''))
    ) STORED,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX listing_brand_idx ON listing (brand);
CREATE INDEX listing_search_idx ON listing USING GIN (search_vector);
CREATE INDEX listing_browse_idx ON listing (published_at DESC) WHERE status = 'available';
CREATE INDEX listing_seller_idx ON listing (seller_sub);

CREATE TABLE photo (
    id           uuid PRIMARY KEY,
    listing_id   uuid        NOT NULL REFERENCES listing (id) ON DELETE CASCADE,
    storage_key  text        NOT NULL,
    content_type text        NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    size_bytes   integer     NOT NULL CHECK (size_bytes BETWEEN 1 AND 10485760),
    position     integer     NOT NULL CHECK (position BETWEEN 0 AND 7),
    state        text        NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'stored')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (listing_id, position)
);
