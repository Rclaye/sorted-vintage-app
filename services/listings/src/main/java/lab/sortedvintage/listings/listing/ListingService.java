package lab.sortedvintage.listings.listing;

import java.util.Set;
import java.util.UUID;
import lab.sortedvintage.listings.seller.SellerProfileRepository;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Create, read, edit and delete (T037, FR-004, FR-006). Edits and deletes lock the row, then check
 * in this order: the caller owns it (403), it is not reserved or sold (409 listing_locked), and for
 * edits the If-Match version is current (409 version_conflict).
 */
@Service
public class ListingService {

    /** States in which the seller can no longer change a listing (data-model.md). */
    static final Set<String> LOCKED = Set.of("reserved", "sold");

    private final ListingRepository listings;
    private final SellerProfileRepository sellers;

    public ListingService(ListingRepository listings, SellerProfileRepository sellers) {
        this.listings = listings;
        this.sellers = sellers;
    }

    @Transactional
    public ListingView create(Caller caller, ListingInput input) {
        sellers.upsert(caller);
        UUID id = UUID.randomUUID();
        listings.insert(id, caller.sub(), input);
        return listings.find(id).orElseThrow();
    }

    /** Drafts are visible only to their seller; deleted listings to no one. */
    @Transactional(readOnly = true)
    public ListingView get(UUID id, Caller caller) {
        ListingView listing = listings.find(id).orElseThrow(() -> ApiException.notFound("The listing"));
        if ("draft".equals(listing.status()) && (caller == null || !caller.sub().equals(listing.sellerSub()))) {
            throw ApiException.notFound("The listing");
        }
        return listing;
    }

    @Transactional
    public ListingView edit(UUID id, Caller caller, int expectedVersion, ListingInput input) {
        ListingRepository.Head head = ownedAndUnlocked(id, caller);
        if (head.version() != expectedVersion) {
            throw new ApiException(HttpStatus.CONFLICT, "version_conflict",
                    "The listing changed since you loaded it (now version " + head.version() + "). Reload and try again.");
        }
        sellers.upsert(caller);
        listings.update(id, input);
        return listings.find(id).orElseThrow();
    }

    @Transactional
    public void delete(UUID id, Caller caller) {
        ownedAndUnlocked(id, caller);
        sellers.upsert(caller);
        listings.markDeleted(id);
    }

    /** Locks the row and checks ownership and state; used by photo changes too. */
    @Transactional
    public ListingRepository.Head ownedAndUnlocked(UUID id, Caller caller) {
        ListingRepository.Head head = listings.lockHead(id).orElseThrow(() -> ApiException.notFound("The listing"));
        if (!head.sellerSub().equals(caller.sub())) {
            throw ApiException.notOwner();
        }
        if (LOCKED.contains(head.status())) {
            throw ApiException.locked();
        }
        return head;
    }

    /** If-Match carries the version, bare or as an entity tag ("3" or W/"3"). */
    static int parseVersion(String ifMatch) {
        String v = ifMatch.strip();
        if (v.startsWith("W/")) {
            v = v.substring(2);
        }
        if (v.length() >= 2 && v.startsWith("\"") && v.endsWith("\"")) {
            v = v.substring(1, v.length() - 1);
        }
        try {
            return Integer.parseInt(v);
        } catch (NumberFormatException e) {
            throw new ApiException(HttpStatus.CONFLICT, "version_conflict",
                    "If-Match must hold the listing's version number.");
        }
    }
}
