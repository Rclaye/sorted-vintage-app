package lab.sortedvintage.listings.photo;

import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** Photo rows (data-model.md): inserted as pending, set to stored once the object is in Garage. */
@Repository
public class PhotoRepository {

    private final JdbcClient jdbc;

    public PhotoRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    /** Positions in use, pending or stored: both count towards the limit of 8. */
    List<Integer> positions(UUID listingId) {
        return jdbc.sql("select position from photo where listing_id = :id order by position")
                .param("id", listingId)
                .query(Integer.class)
                .list();
    }

    void insertPending(UUID id, UUID listingId, String storageKey, String contentType, int sizeBytes, int position) {
        jdbc.sql("""
                insert into photo (id, listing_id, storage_key, content_type, size_bytes, position, state)
                values (:id, :listing, :key, :type, :size, :position, 'pending')
                """)
                .param("id", id)
                .param("listing", listingId)
                .param("key", storageKey)
                .param("type", contentType)
                .param("size", sizeBytes)
                .param("position", position)
                .update();
    }

    void markStored(UUID id) {
        jdbc.sql("update photo set state = 'stored' where id = :id").param("id", id).update();
    }

    /** The storage key of a photo on this listing, if there is one. */
    Optional<String> storageKey(UUID listingId, UUID photoId) {
        return jdbc.sql("select storage_key from photo where id = :id and listing_id = :listing")
                .param("id", photoId)
                .param("listing", listingId)
                .query(String.class)
                .optional();
    }

    void delete(UUID photoId) {
        jdbc.sql("delete from photo where id = :id").param("id", photoId).update();
    }
}
