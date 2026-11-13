package lab.sortedvintage.listings.reservation;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.OffsetDateTime;
import java.util.UUID;

/** Reservation in contracts/listings.openapi.yaml, plus its state. */
public record ReservationView(
        UUID id,
        @JsonProperty("listing_id") UUID listingId,
        @JsonProperty("order_id") UUID orderId,
        @JsonProperty("buyer_sub") UUID buyerSub,
        @JsonProperty("seller_sub") UUID sellerSub,
        @JsonProperty("price_cents") int priceCents,
        String state,
        @JsonProperty("expires_at") OffsetDateTime expiresAt) {
}
