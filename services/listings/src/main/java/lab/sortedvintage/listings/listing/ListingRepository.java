package lab.sortedvintage.listings.listing;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** Listing rows, read with their seller's display name and stored photos. */
@Repository
public class ListingRepository {

    /** The columns every view needs; {@code l} is listing, {@code s} seller_profile. */
    public static final String SELECT = """
            select l.id, l.seller_sub, s.display_name, l.title, l.brand, l.category, l.size, l.condition,
                   l.price_cents, l.description, l.status, l.version, l.published_at
            from listing l join seller_profile s on s.account_sub = l.seller_sub
            """;

    private final JdbcClient jdbc;
    private final PhotoLinks links;

    public ListingRepository(JdbcClient jdbc, PhotoLinks links) {
        this.jdbc = jdbc;
        this.links = links;
    }

    /** The state an edit or delete is checked against, locked for the rest of the transaction. */
    public record Head(UUID sellerSub, String status, int version) {
    }

    public void insert(UUID id, UUID sellerSub, ListingInput in) {
        jdbc.sql("""
                insert into listing (id, seller_sub, title, brand, category, size, condition, price_cents, description)
                values (:id, :seller, :title, :brand, :category, :size, :condition, :price, :description)
                """)
                .param("id", id)
                .param("seller", sellerSub)
                .param("title", in.title())
                .param("brand", in.brand())
                .param("category", in.category())
                .param("size", in.size())
                .param("condition", in.condition())
                .param("price", in.priceCents())
                .param("description", in.descriptionOrEmpty())
                .update();
    }

    /** Any listing that is not deleted, locked FOR UPDATE. */
    public Optional<Head> lockHead(UUID id) {
        return jdbc.sql("select seller_sub, status, version from listing where id = :id and status <> 'deleted' for update")
                .param("id", id)
                .query((rs, n) -> new Head(rs.getObject(1, UUID.class), rs.getString(2), rs.getInt(3)))
                .optional();
    }

    public void update(UUID id, ListingInput in) {
        jdbc.sql("""
                update listing set title = :title, brand = :brand, category = :category, size = :size,
                       condition = :condition, price_cents = :price, description = :description,
                       version = version + 1, updated_at = now()
                where id = :id
                """)
                .param("id", id)
                .param("title", in.title())
                .param("brand", in.brand())
                .param("category", in.category())
                .param("size", in.size())
                .param("condition", in.condition())
                .param("price", in.priceCents())
                .param("description", in.descriptionOrEmpty())
                .update();
    }

    public void markDeleted(UUID id) {
        jdbc.sql("update listing set status = 'deleted', version = version + 1, updated_at = now() where id = :id")
                .param("id", id)
                .update();
    }

    /** A listing that is not deleted, with its stored photos. */
    public Optional<ListingView> find(UUID id) {
        List<ListingView> found = query(SELECT + " where l.id = :id and l.status <> 'deleted'", Map.of("id", id));
        return found.stream().findFirst();
    }

    /** Runs a SELECT built on {@link #SELECT} and attaches each listing's stored photos, in order. */
    public List<ListingView> query(String sql, Map<String, ?> params) {
        List<Row> rows = jdbc.sql(sql).params(params).query(ListingRepository::row).list();
        Map<UUID, List<ListingView.PhotoView>> photos = photos(rows.stream().map(Row::id).toList());
        return rows.stream().map(r -> r.view(photos.getOrDefault(r.id(), List.of()))).toList();
    }

    private Map<UUID, List<ListingView.PhotoView>> photos(Collection<UUID> listingIds) {
        Map<UUID, List<ListingView.PhotoView>> byListing = new HashMap<>();
        if (listingIds.isEmpty()) {
            return byListing;
        }
        jdbc.sql("""
                select listing_id, id, storage_key, content_type, position from photo
                where listing_id in (:ids) and state = 'stored' order by listing_id, position
                """)
                .param("ids", listingIds)
                .query((rs, n) -> {
                    byListing.computeIfAbsent(rs.getObject(1, UUID.class), k -> new ArrayList<>())
                            .add(new ListingView.PhotoView(rs.getObject(2, UUID.class), links.url(rs.getString(3)),
                                    rs.getString(4), rs.getInt(5)));
                    return null;
                })
                .list();
        return byListing;
    }

    private record Row(UUID id, UUID sellerSub, String sellerName, String title, String brand, String category,
            String size, String condition, int priceCents, String description, String status, int version,
            OffsetDateTime publishedAt) {

        ListingView view(List<ListingView.PhotoView> photos) {
            return new ListingView(id, sellerSub, sellerName, title, brand, category, size, condition, priceCents,
                    description, status, version, photos, publishedAt);
        }
    }

    private static Row row(ResultSet rs, int n) throws SQLException {
        return new Row(rs.getObject("id", UUID.class), rs.getObject("seller_sub", UUID.class),
                rs.getString("display_name"), rs.getString("title"), rs.getString("brand"),
                rs.getString("category"), rs.getString("size"), rs.getString("condition"),
                rs.getInt("price_cents"), rs.getString("description"), rs.getString("status"),
                rs.getInt("version"), rs.getObject("published_at", OffsetDateTime.class));
    }
}
