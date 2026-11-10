package lab.sortedvintage.listings.search;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import lab.sortedvintage.listings.listing.ListingRepository;
import lab.sortedvintage.listings.listing.ListingView;
import lab.sortedvintage.listings.web.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Repository;

/**
 * T040, browse and search (FR-007): only available listings, newest published first, full-text
 * {@code q} over search_vector (the same 'simple' configuration V1 builds it with), exact filters,
 * and keyset pagination on (published_at, id). The cursor is opaque to clients: base64url of the
 * last row's published_at in epoch microseconds and its id, so a page never repeats or skips a
 * listing when new ones are published meanwhile.
 */
@Repository
public class SearchRepository {

    static final int DEFAULT_LIMIT = 24;
    static final int MAX_LIMIT = 60;

    private final ListingRepository listings;

    public SearchRepository(ListingRepository listings) {
        this.listings = listings;
    }

    public record Criteria(String q, String brand, String size, String category, Integer minPriceCents,
            Integer maxPriceCents, String cursor, Integer limit) {
    }

    public record Page(List<ListingView> items, String nextCursor) {
    }

    public Page search(Criteria c) {
        int limit = c.limit() == null ? DEFAULT_LIMIT : c.limit();
        if (limit < 1 || limit > MAX_LIMIT) {
            throw invalid("limit must be between 1 and " + MAX_LIMIT + ".");
        }
        StringBuilder sql = new StringBuilder(ListingRepository.SELECT).append(" where l.status = 'available'");
        Map<String, Object> params = new HashMap<>();
        if (notBlank(c.q())) {
            sql.append(" and l.search_vector @@ websearch_to_tsquery('simple', :q)");
            params.put("q", c.q().strip());
        }
        if (notBlank(c.brand())) {
            sql.append(" and l.brand = :brand");
            params.put("brand", c.brand().strip());
        }
        if (notBlank(c.size())) {
            sql.append(" and l.size = :size");
            params.put("size", c.size().strip());
        }
        if (notBlank(c.category())) {
            sql.append(" and l.category = :category");
            params.put("category", c.category().strip());
        }
        if (c.minPriceCents() != null) {
            sql.append(" and l.price_cents >= :minPrice");
            params.put("minPrice", c.minPriceCents());
        }
        if (c.maxPriceCents() != null) {
            sql.append(" and l.price_cents <= :maxPrice");
            params.put("maxPrice", c.maxPriceCents());
        }
        if (notBlank(c.cursor())) {
            Cursor after = Cursor.decode(c.cursor());
            sql.append(" and (l.published_at, l.id) < (:afterPublished, :afterId)");
            params.put("afterPublished", after.publishedAt());
            params.put("afterId", after.id());
        }
        sql.append(" order by l.published_at desc, l.id desc limit :fetch");
        params.put("fetch", limit + 1);

        List<ListingView> rows = listings.query(sql.toString(), params);
        if (rows.size() <= limit) {
            return new Page(rows, null);
        }
        List<ListingView> page = rows.subList(0, limit);
        ListingView last = page.getLast();
        return new Page(List.copyOf(page), new Cursor(last.publishedAt(), last.id()).encode());
    }

    record Cursor(OffsetDateTime publishedAt, UUID id) {

        String encode() {
            long micros = ChronoUnit.MICROS.between(Instant.EPOCH, publishedAt.toInstant());
            return Base64.getUrlEncoder().withoutPadding()
                    .encodeToString((micros + ":" + id).getBytes(StandardCharsets.UTF_8));
        }

        static Cursor decode(String token) {
            try {
                String[] parts = new String(Base64.getUrlDecoder().decode(token.strip()), StandardCharsets.UTF_8)
                        .split(":", 2);
                Instant at = Instant.EPOCH.plus(Long.parseLong(parts[0]), ChronoUnit.MICROS);
                return new Cursor(OffsetDateTime.ofInstant(at, ZoneOffset.UTC), UUID.fromString(parts[1]));
            } catch (RuntimeException e) {
                throw invalid("The cursor is not one this service issued.");
            }
        }
    }

    private static boolean notBlank(String s) {
        return s != null && !s.isBlank();
    }

    private static ApiException invalid(String message) {
        return new ApiException(HttpStatus.UNPROCESSABLE_CONTENT, "validation_failed", message);
    }
}
