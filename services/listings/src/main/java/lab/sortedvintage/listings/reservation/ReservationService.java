package lab.sortedvintage.listings.reservation;

import java.util.List;
import java.util.Optional;
import java.util.UUID;
import lab.sortedvintage.listings.web.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * T059: reserve, consume and release (FR-009, research R8). A reservation holds an available
 * listing for one buyer's order for ten minutes. Reserving is one conditional update on the
 * listing ({@code status = 'available'}) in the same transaction as the insert, so two buyers can
 * never both win; the partial unique index on active reservations backs that up. Consume and
 * release are idempotent, because Orders retries them.
 */
@Service
public class ReservationService {

    static final String HOLD = "10 minutes";

    private final JdbcClient jdbc;

    public ReservationService(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    /** One reservation row with its listing's seller, locked when asked. */
    record Row(UUID id, UUID listingId, UUID orderId, UUID buyerSub, UUID sellerSub, int priceCents, String state,
            boolean expired, java.time.OffsetDateTime expiresAt) {

        ReservationView view() {
            return new ReservationView(id, listingId, orderId, buyerSub, sellerSub, priceCents, state, expiresAt);
        }
    }

    private static final String ROW = """
            select r.id, r.listing_id, r.order_id, r.buyer_sub, l.seller_sub, r.price_cents, r.state,
                   r.expires_at <= now() as expired, r.expires_at
            from reservation r join listing l on l.id = r.listing_id
            """;

    private Optional<Row> row(String where, String param, UUID value, boolean lock) {
        return jdbc.sql(ROW + " where " + where + (lock ? " for update of r" : ""))
                .param(param, value)
                .query((rs, n) -> new Row(rs.getObject(1, UUID.class), rs.getObject(2, UUID.class),
                        rs.getObject(3, UUID.class), rs.getObject(4, UUID.class), rs.getObject(5, UUID.class),
                        rs.getInt(6), rs.getString(7), rs.getBoolean(8),
                        rs.getObject(9, java.time.OffsetDateTime.class)))
                .optional();
    }

    @Transactional
    public ReservationView reserve(UUID listingId, UUID buyer, UUID orderId) {
        // A retry for the same order gets its reservation back instead of a 409.
        Optional<Row> existing = row("r.order_id = :order", "order", orderId, false);
        if (existing.isPresent()) {
            Row r = existing.get();
            if (r.listingId().equals(listingId) && r.buyerSub().equals(buyer) && "active".equals(r.state())) {
                return r.view();
            }
            throw notAvailable();
        }
        record Head(UUID sellerSub, String status, int price) {
        }
        Head head = jdbc.sql("select seller_sub, status, price_cents from listing where id = :id and status <> 'deleted'")
                .param("id", listingId)
                .query((rs, n) -> new Head(rs.getObject(1, UUID.class), rs.getString(2), rs.getInt(3)))
                .optional()
                .orElseThrow(() -> ApiException.notFound("The listing"));
        if (head.sellerSub().equals(buyer)) {
            throw new ApiException(HttpStatus.FORBIDDEN, "own_listing", "You cannot buy your own listing.");
        }
        int taken = jdbc.sql("""
                update listing set status = 'reserved', updated_at = now()
                where id = :id and status = 'available'
                """).param("id", listingId).update();
        if (taken == 0) {
            throw notAvailable();
        }
        UUID id = UUID.randomUUID();
        jdbc.sql("""
                insert into reservation (id, listing_id, buyer_sub, order_id, price_cents, expires_at)
                select :id, l.id, :buyer, :order, l.price_cents, now() + interval '%s'
                from listing l where l.id = :listing
                """.formatted(HOLD))
                .param("id", id)
                .param("buyer", buyer)
                .param("order", orderId)
                .param("listing", listingId)
                .update();
        return row("r.id = :id", "id", id, false).orElseThrow().view();
    }

    /**
     * Marks the listing sold. Idempotent for a consumed reservation; 409 reservation_released for a
     * released one, or for one past its expiry the sweeper has not reached yet (it is released
     * here, and that release is kept: Orders then refunds).
     */
    @Transactional(noRollbackFor = ApiException.class)
    public ReservationView consume(UUID id) {
        Row r = row("r.id = :id", "id", id, true).orElseThrow(() -> ApiException.notFound("The reservation"));
        switch (r.state()) {
            case "consumed" -> {
                return r.view();
            }
            case "released" -> throw released();
            default -> {
                if (r.expired()) {
                    releaseRow(r);
                    throw released();
                }
            }
        }
        jdbc.sql("update reservation set state = 'consumed', closed_at = now() where id = :id").param("id", id).update();
        jdbc.sql("update listing set status = 'sold', updated_at = now() where id = :id and status = 'reserved'")
                .param("id", r.listingId()).update();
        jdbc.sql("update seller_profile set sold_count = sold_count + 1 where account_sub = :seller")
                .param("seller", r.sellerSub()).update();
        return row("r.id = :id", "id", id, false).orElseThrow().view();
    }

    /** Puts the listing back on sale. Idempotent; a consumed reservation is left as it is (the item is sold). */
    @Transactional
    public ReservationView release(UUID id) {
        Row r = row("r.id = :id", "id", id, true).orElseThrow(() -> ApiException.notFound("The reservation"));
        if ("active".equals(r.state())) {
            releaseRow(r);
        }
        return row("r.id = :id", "id", id, false).orElseThrow().view();
    }

    /** T060: releases active reservations past their expiry; returns how many. Safe with several replicas. */
    @Transactional
    public int releaseExpired(int limit) {
        List<UUID> ids = jdbc.sql("""
                select id from reservation where state = 'active' and expires_at <= now()
                order by expires_at limit :limit for update skip locked
                """).param("limit", limit).query(UUID.class).list();
        for (UUID id : ids) {
            row("r.id = :id", "id", id, false).ifPresent(this::releaseRow);
        }
        return ids.size();
    }

    private void releaseRow(Row r) {
        jdbc.sql("update reservation set state = 'released', closed_at = now() where id = :id and state = 'active'")
                .param("id", r.id()).update();
        jdbc.sql("update listing set status = 'available', updated_at = now() where id = :id and status = 'reserved'")
                .param("id", r.listingId()).update();
    }

    private static ApiException notAvailable() {
        return new ApiException(HttpStatus.CONFLICT, "not_available", "This item is no longer available.");
    }

    private static ApiException released() {
        return new ApiException(HttpStatus.CONFLICT, "reservation_released",
                "The reservation was released or expired; the item was not sold.");
    }
}
