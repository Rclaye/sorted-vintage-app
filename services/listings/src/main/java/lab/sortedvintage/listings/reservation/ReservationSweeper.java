package lab.sortedvintage.listings.reservation;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * T060: every 30 seconds, releases active reservations past their expiry, so an abandoned
 * checkout puts the item back on sale within about ten and a half minutes. Rows are locked with
 * SKIP LOCKED, so two replicas never release the same one twice.
 */
@Component
public class ReservationSweeper {

    static final int BATCH = 100;

    private static final Logger log = LoggerFactory.getLogger(ReservationSweeper.class);

    private final ReservationService reservations;

    public ReservationSweeper(ReservationService reservations) {
        this.reservations = reservations;
    }

    @Scheduled(initialDelayString = "${listings.reservations.sweep-interval:PT30S}",
            fixedDelayString = "${listings.reservations.sweep-interval:PT30S}")
    public void scheduled() {
        try {
            run();
        } catch (RuntimeException e) {
            log.error("reservation sweep failed; it runs again next interval", e);
        }
    }

    /** Releases every expired reservation; returns how many. */
    public int run() {
        int released = 0;
        int batch;
        do {
            batch = reservations.releaseExpired(BATCH);
            released += batch;
        } while (batch == BATCH);
        if (released > 0) {
            log.info("released {} expired reservations", released);
        }
        return released;
    }
}
