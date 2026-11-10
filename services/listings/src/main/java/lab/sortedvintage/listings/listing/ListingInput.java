package lab.sortedvintage.listings.listing;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * ListingInput in contracts/listings.openapi.yaml; the same limits as V1's CHECK constraints
 * (data-model.md). Category and condition are strings here so an unknown value is a 422, not an
 * unreadable body. Sizes are not checked against a per-category list: data-model.md mentions one
 * but no list is defined yet.
 */
public record ListingInput(
        @NotNull @Size(min = 3, max = 80) String title,
        @NotBlank @Size(max = 40) String brand,
        @NotNull @Pattern(regexp = "tops|bottoms|dresses|outerwear|shoes|accessories") String category,
        @NotBlank @Size(max = 10) String size,
        @NotNull @Pattern(regexp = "new_with_tags|like_new|good|fair") String condition,
        @JsonProperty("price_cents") @NotNull @Min(100) @Max(1_000_000) Integer priceCents,
        @Size(max = 2000) String description) {

    String descriptionOrEmpty() {
        return description == null ? "" : description;
    }
}
