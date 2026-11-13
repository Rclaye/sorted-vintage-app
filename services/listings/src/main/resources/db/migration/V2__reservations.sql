-- Reservation (data-model.md; tasks.md T059, FR-009). A buyer's ten-minute hold on an available
-- listing while they pay. At most one active reservation per listing, enforced by the database as
-- well as by the conditional update that takes the hold.

CREATE TABLE reservation (
    id          uuid PRIMARY KEY,
    listing_id  uuid        NOT NULL REFERENCES listing (id),
    buyer_sub   uuid        NOT NULL,
    order_id    uuid        NOT NULL UNIQUE,
    price_cents integer     NOT NULL CHECK (price_cents BETWEEN 100 AND 1000000),
    state       text        NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released', 'consumed')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    closed_at   timestamptz
);

CREATE UNIQUE INDEX reservation_one_active_per_listing ON reservation (listing_id) WHERE state = 'active';
-- The sweeper's query (T060): active holds past their expiry.
CREATE INDEX reservation_expiry_idx ON reservation (expires_at) WHERE state = 'active';
