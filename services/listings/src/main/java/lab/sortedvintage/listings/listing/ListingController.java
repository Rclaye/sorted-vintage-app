package lab.sortedvintage.listings.listing;

import jakarta.validation.Valid;
import java.util.UUID;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

/** POST /listings, and GET, PATCH and DELETE /listings/{listing_id} (contracts/listings.openapi.yaml). */
@RestController
public class ListingController {

    private final ListingService service;
    private final PublishService publishing;

    public ListingController(ListingService service, PublishService publishing) {
        this.service = service;
        this.publishing = publishing;
    }

    /** T046: 409 photos_required or payout_onboarding_required (the BFF sends the seller to onboarding). */
    @PostMapping("/listings/{id}/publish")
    ResponseEntity<ListingView> publish(@PathVariable UUID id, Authentication auth) {
        return withVersion(ResponseEntity.ok(), publishing.publish(id, Caller.required(auth)));
    }

    @PostMapping("/listings")
    ResponseEntity<ListingView> create(@Valid @RequestBody ListingInput input, Authentication auth) {
        ListingView created = service.create(Caller.required(auth), input);
        return withVersion(ResponseEntity.status(HttpStatus.CREATED), created);
    }

    @GetMapping("/listings/{id}")
    ResponseEntity<ListingView> get(@PathVariable UUID id, Authentication auth) {
        ListingView listing = service.get(id, Caller.of(auth));
        return withVersion(ResponseEntity.ok(), listing);
    }

    @PatchMapping("/listings/{id}")
    ResponseEntity<ListingView> edit(@PathVariable UUID id, @RequestHeader(HttpHeaders.IF_MATCH) String ifMatch,
            @Valid @RequestBody ListingInput input, Authentication auth) {
        ListingView edited = service.edit(id, Caller.required(auth), ListingService.parseVersion(ifMatch), input);
        return withVersion(ResponseEntity.ok(), edited);
    }

    @DeleteMapping("/listings/{id}")
    ResponseEntity<Void> delete(@PathVariable UUID id, Authentication auth) {
        service.delete(id, Caller.required(auth));
        return ResponseEntity.noContent().build();
    }

    /** The version also goes out as an ETag, so a client can echo it in If-Match. */
    private static ResponseEntity<ListingView> withVersion(ResponseEntity.BodyBuilder builder, ListingView listing) {
        return builder.eTag("\"" + listing.version() + "\"").body(listing);
    }
}
