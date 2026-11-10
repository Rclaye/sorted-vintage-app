package lab.sortedvintage.listings.photo;

import java.util.List;
import java.util.UUID;
import lab.sortedvintage.listings.listing.ListingService;
import lab.sortedvintage.listings.listing.ListingView;
import lab.sortedvintage.listings.listing.PhotoLinks;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.services.s3.S3Client;

/**
 * Photo upload and removal (T038, FR-005). An upload is checked (owner, not reserved or sold,
 * at most 8, at most 10,485,760 bytes, JPEG, PNG or WebP by its bytes), then the row is committed
 * as pending, the object is written to listings/&lt;listing_id&gt;/&lt;photo_id&gt;, and only then
 * is the row set to stored. A failed check writes nothing; an interrupted upload leaves a pending
 * row that publishing ignores and the cleanup job (T039) removes.
 */
@Service
public class PhotoService {

    static final int MAX_BYTES = 10_485_760;
    static final int MAX_PHOTOS = 8;

    private static final Logger log = LoggerFactory.getLogger(PhotoService.class);

    private final ListingService listings;
    private final PhotoRepository photos;
    private final PhotoLinks links;
    private final S3Client s3;
    private final String bucket;
    private final TransactionTemplate tx;

    public PhotoService(ListingService listings, PhotoRepository photos, PhotoLinks links, S3Client s3,
            @Value("${listings.s3.bucket}") String bucket, TransactionTemplate tx) {
        this.listings = listings;
        this.photos = photos;
        this.links = links;
        this.s3 = s3;
        this.bucket = bucket;
        this.tx = tx;
    }

    public ListingView.PhotoView upload(UUID listingId, Caller caller, byte[] bytes, Integer requestedPosition) {
        if (requestedPosition != null && (requestedPosition < 0 || requestedPosition >= MAX_PHOTOS)) {
            throw new ApiException(HttpStatus.UNPROCESSABLE_CONTENT, "validation_failed",
                    "position must be between 0 and 7.");
        }
        UUID photoId = UUID.randomUUID();
        String key = "listings/" + listingId + "/" + photoId;

        Pending pending = tx.execute(status -> {
            listings.ownedAndUnlocked(listingId, caller);
            List<Integer> used = photos.positions(listingId);
            if (used.size() >= MAX_PHOTOS) {
                throw new ApiException(HttpStatus.CONFLICT, "photo_limit", "A listing can have at most 8 photos.");
            }
            if (bytes.length > MAX_BYTES) {
                throw tooLarge();
            }
            ImageType type = ImageType.detect(bytes).orElseThrow(() -> new ApiException(
                    HttpStatus.UNSUPPORTED_MEDIA_TYPE, "photo_type_rejected",
                    "Photos must be JPEG, PNG or WebP images."));
            int position = requestedPosition != null ? requestedPosition : firstFree(used);
            if (used.contains(position)) {
                throw new ApiException(HttpStatus.CONFLICT, "photo_position_taken",
                        "Another photo is already at position " + position + ".");
            }
            photos.insertPending(photoId, listingId, key, type.contentType, bytes.length, position);
            return new Pending(type, position);
        });

        try {
            s3.putObject(r -> r.bucket(bucket).key(key).contentType(pending.type().contentType)
                    .contentLength((long) bytes.length), RequestBody.fromBytes(bytes));
        } catch (RuntimeException e) {
            log.error("photo {} could not be written to the bucket", photoId, e);
            tx.executeWithoutResult(status -> photos.delete(photoId));
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "storage_unavailable",
                    "The photo could not be stored. Try again in a moment.");
        }
        tx.executeWithoutResult(status -> photos.markStored(photoId));
        return new ListingView.PhotoView(photoId, links.url(key), pending.type().contentType, pending.position());
    }

    public void remove(UUID listingId, UUID photoId, Caller caller) {
        String key = tx.execute(status -> {
            listings.ownedAndUnlocked(listingId, caller);
            String storageKey = photos.storageKey(listingId, photoId)
                    .orElseThrow(() -> ApiException.notFound("The photo"));
            photos.delete(photoId);
            return storageKey;
        });
        try {
            s3.deleteObject(r -> r.bucket(bucket).key(key));
        } catch (RuntimeException e) {
            // The row is gone, so no listing shows the photo; the object is only wasted space.
            log.warn("photo {} removed but its object {} could not be deleted", photoId, key, e);
        }
    }

    static ApiException tooLarge() {
        return new ApiException(HttpStatus.CONTENT_TOO_LARGE, "photo_too_large", "Photos can be at most 10 MB.");
    }

    private static int firstFree(List<Integer> used) {
        for (int p = 0; p < MAX_PHOTOS; p++) {
            if (!used.contains(p)) {
                return p;
            }
        }
        throw new IllegalStateException("no free position although fewer than 8 photos");
    }

    private record Pending(ImageType type, int position) {
    }
}
