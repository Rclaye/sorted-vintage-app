package lab.sortedvintage.listings.photo;

import java.time.Duration;
import java.util.UUID;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.CacheControl;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;

/**
 * GET /photos/listings/{listing_id}/{photo_id}: the bytes behind a Photo's url (PhotoLinks).
 * Garage is reachable only inside the cluster, so the BFF forwards /photos/* here and Listings
 * stays the only service holding S3 keys (decided 2026-11-11, Ticket 2.6). Stored photos of a
 * published listing are public and cacheable for good (a photo's key never changes); a draft's
 * photos are visible to its seller only; anything else is 404.
 */
@RestController
public class PhotoFileController {

    private final JdbcClient jdbc;
    private final S3Client s3;
    private final String bucket;

    public PhotoFileController(JdbcClient jdbc, S3Client s3, @Value("${listings.s3.bucket}") String bucket) {
        this.jdbc = jdbc;
        this.s3 = s3;
        this.bucket = bucket;
    }

    private record Found(String storageKey, String contentType, String listingStatus, UUID sellerSub) {
    }

    @GetMapping("/photos/listings/{listingId}/{photoId}")
    ResponseEntity<byte[]> photo(@PathVariable UUID listingId, @PathVariable UUID photoId, Authentication auth) {
        Found found = jdbc.sql("""
                select p.storage_key, p.content_type, l.status, l.seller_sub
                from photo p join listing l on l.id = p.listing_id
                where p.id = :photo and p.listing_id = :listing and p.state = 'stored' and l.status <> 'deleted'
                """)
                .param("photo", photoId)
                .param("listing", listingId)
                .query((rs, n) -> new Found(rs.getString(1), rs.getString(2), rs.getString(3),
                        rs.getObject(4, UUID.class)))
                .optional()
                .orElseThrow(() -> ApiException.notFound("The photo"));

        boolean draft = "draft".equals(found.listingStatus());
        if (draft) {
            Caller caller = Caller.of(auth);
            if (caller == null || !caller.sub().equals(found.sellerSub())) {
                throw ApiException.notFound("The photo");
            }
        }
        byte[] bytes;
        try {
            bytes = s3.getObjectAsBytes(r -> r.bucket(bucket).key(found.storageKey())).asByteArray();
        } catch (NoSuchKeyException e) {
            throw ApiException.notFound("The photo");
        }
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(found.contentType()))
                .cacheControl(draft
                        ? CacheControl.noStore()
                        : CacheControl.maxAge(Duration.ofDays(365)).cachePublic().immutable())
                .header("X-Content-Type-Options", "nosniff")
                .body(bytes);
    }
}
