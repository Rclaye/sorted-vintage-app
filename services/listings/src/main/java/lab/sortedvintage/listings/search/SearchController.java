package lab.sortedvintage.listings.search;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Set;
import lab.sortedvintage.listings.listing.ListingView;
import lab.sortedvintage.listings.web.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** GET /listings: public browse and search (FR-007). */
@RestController
public class SearchController {

    static final Set<String> CATEGORIES = Set.of("tops", "bottoms", "dresses", "outerwear", "shoes", "accessories");

    private final SearchRepository search;

    public SearchController(SearchRepository search) {
        this.search = search;
    }

    /** The contract's page: items, and next_cursor, null on the last page. */
    public record PageView(List<ListingView> items,
            @JsonProperty("next_cursor") @JsonInclude(JsonInclude.Include.ALWAYS) String nextCursor) {
    }

    @GetMapping("/listings")
    @Transactional(readOnly = true)
    PageView browse(@RequestParam(required = false) String q,
            @RequestParam(required = false) String brand,
            @RequestParam(required = false) String size,
            @RequestParam(required = false) String category,
            @RequestParam(name = "min_price_cents", required = false) Integer minPriceCents,
            @RequestParam(name = "max_price_cents", required = false) Integer maxPriceCents,
            @RequestParam(required = false) String cursor,
            @RequestParam(required = false) Integer limit) {
        if (category != null && !category.isBlank() && !CATEGORIES.contains(category.strip())) {
            throw new ApiException(HttpStatus.UNPROCESSABLE_CONTENT, "validation_failed",
                    "category must be one of " + String.join(", ", CATEGORIES.stream().sorted().toList()) + ".");
        }
        SearchRepository.Page page = search.search(new SearchRepository.Criteria(
                q, brand, size, category, minPriceCents, maxPriceCents, cursor, limit));
        return new PageView(page.items(), page.nextCursor());
    }
}
