package lab.sortedvintage.listings.reservation;

import jakarta.validation.Valid;
import java.util.UUID;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * POST /listings/{id}/reservations, called by Orders with the buyer's own token, and
 * POST /reservations/{id}/consume and /release, which only Orders' client-credentials token may call
 * (contracts/listings.openapi.yaml, T059).
 */
@RestController
public class ReservationController {

    private final ReservationService reservations;
    private final String ordersClient;

    public ReservationController(ReservationService reservations,
            @Value("${listings.reservations.orders-client:orders}") String ordersClient) {
        this.reservations = reservations;
        this.ordersClient = ordersClient;
    }

    @PostMapping("/listings/{id}/reservations")
    ResponseEntity<ReservationView> reserve(@PathVariable UUID id, @Valid @RequestBody ReserveInput input,
            Authentication auth) {
        ReservationView held = reservations.reserve(id, Caller.required(auth).sub(), input.orderId());
        return ResponseEntity.status(HttpStatus.CREATED).body(held);
    }

    @PostMapping("/reservations/{id}/consume")
    ReservationView consume(@PathVariable UUID id, Authentication auth) {
        requireOrders(auth);
        return reservations.consume(id);
    }

    @PostMapping("/reservations/{id}/release")
    ReservationView release(@PathVariable UUID id, Authentication auth) {
        requireOrders(auth);
        return reservations.release(id);
    }

    private void requireOrders(Authentication auth) {
        String azp = auth instanceof JwtAuthenticationToken jwt ? jwt.getToken().getClaimAsString("azp") : null;
        if (!ordersClient.equals(azp)) {
            throw new ApiException(HttpStatus.FORBIDDEN, "forbidden", "Only Orders may settle a reservation.");
        }
    }
}
