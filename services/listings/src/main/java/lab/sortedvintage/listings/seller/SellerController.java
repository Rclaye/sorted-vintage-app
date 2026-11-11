package lab.sortedvintage.listings.seller;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import lab.sortedvintage.listings.listing.ListingRepository;
import lab.sortedvintage.listings.listing.ListingView;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.security.core.Authentication;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

/**
 * T041, GET /sellers/{account_sub}: the public profile with its available listings (newest
 * published first) and sold ones (most recently sold first), FR-008. Each list holds at most 60,
 * the browse page maximum. Deleted listings are never shown; drafts only to the seller themself,
 * in a drafts list that is absent for everyone else (Ticket 2.6, for the BFF's /sell page).
 */
@RestController
public class SellerController {

    static final int MAX_EACH = 60;

    private final JdbcClient jdbc;
    private final ListingRepository listings;

    public SellerController(JdbcClient jdbc, ListingRepository listings) {
        this.jdbc = jdbc;
        this.listings = listings;
    }

    public record SellerView(
            @JsonProperty("account_sub") UUID accountSub,
            @JsonProperty("display_name") String displayName,
            @JsonProperty("sold_count") int soldCount,
            List<ListingView> available,
            List<ListingView> sold,
            @JsonInclude(JsonInclude.Include.NON_NULL) List<ListingView> drafts) {
    }

    @GetMapping("/sellers/{sub}")
    @Transactional(readOnly = true)
    SellerView profile(@PathVariable UUID sub, Authentication auth) {
        record Profile(String displayName, int soldCount) {
        }
        Profile profile = jdbc.sql("select display_name, sold_count from seller_profile where account_sub = :sub")
                .param("sub", sub)
                .query((rs, n) -> new Profile(rs.getString(1), rs.getInt(2)))
                .optional()
                .orElseThrow(() -> ApiException.notFound("The seller"));
        List<ListingView> available = listings.query(ListingRepository.SELECT
                + " where l.seller_sub = :sub and l.status = 'available'"
                + " order by l.published_at desc, l.id desc limit :max", Map.of("sub", sub, "max", MAX_EACH));
        List<ListingView> sold = listings.query(ListingRepository.SELECT
                + " where l.seller_sub = :sub and l.status = 'sold'"
                + " order by l.updated_at desc, l.id desc limit :max", Map.of("sub", sub, "max", MAX_EACH));
        Caller caller = Caller.of(auth);
        List<ListingView> drafts = caller != null && caller.sub().equals(sub)
                ? listings.query(ListingRepository.SELECT
                        + " where l.seller_sub = :sub and l.status = 'draft'"
                        + " order by l.updated_at desc, l.id desc limit :max", Map.of("sub", sub, "max", MAX_EACH))
                : null;
        return new SellerView(sub, profile.displayName(), profile.soldCount(), available, sold, drafts);
    }
}
