package lab.sortedvintage.listings.listing;

import java.util.UUID;
import lab.sortedvintage.listings.payout.PayoutStatusClient;
import lab.sortedvintage.listings.seller.SellerProfileRepository;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * T046, POST /listings/{id}/publish (FR-004, FR-014): a draft with 1 to 8 stored photos whose
 * seller's payout status is ready becomes available. Payments is asked between two short
 * transactions, so no row stays locked during the network call; the second transaction checks
 * everything again before publishing.
 */
@Service
public class PublishService {

    static final int MIN_PHOTOS = 1;
    static final int MAX_PHOTOS = 8;

    private final ListingService listings;
    private final ListingRepository repository;
    private final SellerProfileRepository sellers;
    private final PayoutStatusClient payments;
    private final JdbcClient jdbc;
    private final TransactionTemplate tx;

    public PublishService(ListingService listings, ListingRepository repository, SellerProfileRepository sellers,
            PayoutStatusClient payments, JdbcClient jdbc, TransactionTemplate tx) {
        this.listings = listings;
        this.repository = repository;
        this.sellers = sellers;
        this.payments = payments;
        this.jdbc = jdbc;
        this.tx = tx;
    }

    public ListingView publish(UUID id, Caller caller) {
        tx.executeWithoutResult(status -> checkPublishable(id, caller));

        if (!"ready".equals(payments.payoutStatus(caller.sub()))) {
            throw new ApiException(HttpStatus.CONFLICT, "payout_onboarding_required",
                    "Finish setting up payouts with Stripe before publishing.");
        }

        return tx.execute(status -> {
            checkPublishable(id, caller);
            sellers.upsert(caller);
            jdbc.sql("""
                    update listing set status = 'available', published_at = now(), version = version + 1,
                           updated_at = now()
                    where id = :id and status = 'draft'
                    """).param("id", id).update();
            return repository.find(id).orElseThrow();
        });
    }

    /** Locks the row: the caller owns it, it is a draft, and it has 1 to 8 stored photos. */
    private void checkPublishable(UUID id, Caller caller) {
        ListingRepository.Head head = listings.ownedAndUnlocked(id, caller);
        if (!"draft".equals(head.status())) {
            throw new ApiException(HttpStatus.CONFLICT, "not_a_draft", "This listing is already published.");
        }
        Integer stored = jdbc.sql("select count(*) from photo where listing_id = :id and state = 'stored'")
                .param("id", id).query(Integer.class).single();
        if (stored < MIN_PHOTOS || stored > MAX_PHOTOS) {
            throw new ApiException(HttpStatus.CONFLICT, "photos_required",
                    "A listing needs 1 to 8 photos before it can be published.");
        }
    }
}
