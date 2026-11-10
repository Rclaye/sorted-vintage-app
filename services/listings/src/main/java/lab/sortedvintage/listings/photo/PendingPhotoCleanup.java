package lab.sortedvintage.listings.photo;

import java.time.Duration;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.services.s3.S3Client;

/**
 * T039: deletes photo rows still pending an hour after they were created, and their objects
 * (data-model.md, "Interrupted upload"). The object goes first, then the row, so a row is never
 * left pointing at nothing it still promises. Deleting a missing object succeeds in S3, so a run
 * that stops halfway is finished by the next one, and two replicas running at once do no harm.
 */
@Component
public class PendingPhotoCleanup {

    static final int BATCH = 100;

    private static final Logger log = LoggerFactory.getLogger(PendingPhotoCleanup.class);

    private final JdbcClient jdbc;
    private final S3Client s3;
    private final String bucket;
    private final Duration maxAge;

    public PendingPhotoCleanup(JdbcClient jdbc, S3Client s3, @Value("${listings.s3.bucket}") String bucket,
            @Value("${listings.photos.pending-max-age:PT1H}") Duration maxAge) {
        this.jdbc = jdbc;
        this.s3 = s3;
        this.bucket = bucket;
        this.maxAge = maxAge;
    }

    @Scheduled(initialDelayString = "${listings.photos.cleanup-interval:PT10M}",
            fixedDelayString = "${listings.photos.cleanup-interval:PT10M}")
    public void scheduled() {
        try {
            run();
        } catch (RuntimeException e) {
            log.error("pending photo cleanup failed; it runs again next interval", e);
        }
    }

    /** Removes every pending photo older than the maximum age; returns how many. */
    public int run() {
        int removed = 0;
        List<Stale> batch;
        do {
            batch = jdbc.sql("""
                    select id, storage_key from photo
                    where state = 'pending' and created_at < now() - make_interval(secs => :age)
                    order by created_at limit :limit
                    """)
                    .param("age", maxAge.toSeconds())
                    .param("limit", BATCH)
                    .query((rs, n) -> new Stale(rs.getObject(1, UUID.class), rs.getString(2)))
                    .list();
            for (Stale stale : batch) {
                s3.deleteObject(r -> r.bucket(bucket).key(stale.key()));
                removed += jdbc.sql("delete from photo where id = :id and state = 'pending'")
                        .param("id", stale.id())
                        .update();
            }
        } while (batch.size() == BATCH);
        if (removed > 0) {
            log.info("removed {} pending photos older than {}", removed, maxAge);
        }
        return removed;
    }

    private record Stale(UUID id, String key) {
    }
}
