package lab.sortedvintage.listings.listing;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

/** Listing in contracts/listings.openapi.yaml. */
public record ListingView(
        UUID id,
        @JsonProperty("seller_sub") UUID sellerSub,
        @JsonProperty("seller_display_name") String sellerDisplayName,
        String title,
        String brand,
        String category,
        String size,
        String condition,
        @JsonProperty("price_cents") int priceCents,
        String description,
        String status,
        int version,
        List<PhotoView> photos,
        @JsonProperty("published_at") OffsetDateTime publishedAt) {

    /** Photo in the contract; {@code url} is where the BFF serves the image (see PhotoLinks). */
    public record PhotoView(
            UUID id,
            String url,
            @JsonProperty("content_type") String contentType,
            int position) {
    }
}
