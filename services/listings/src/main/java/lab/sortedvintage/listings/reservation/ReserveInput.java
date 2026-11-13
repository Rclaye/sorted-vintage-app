package lab.sortedvintage.listings.reservation;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotNull;
import java.util.UUID;

/** The body of POST /listings/{id}/reservations: the order the hold is for. */
public record ReserveInput(@NotNull @JsonProperty("order_id") UUID orderId) {
}
