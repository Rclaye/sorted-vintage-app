package lab.sortedvintage.listings.web;

import org.springframework.http.HttpStatus;

/** An error answered with the contract's body: {"error": "<code>", "message": "<text>"}. */
public class ApiException extends RuntimeException {

    private final HttpStatus status;
    private final String code;

    public ApiException(HttpStatus status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    public HttpStatus status() {
        return status;
    }

    public String code() {
        return code;
    }

    public static ApiException notFound(String what) {
        return new ApiException(HttpStatus.NOT_FOUND, "not_found", what + " was not found.");
    }

    public static ApiException notOwner() {
        return new ApiException(HttpStatus.FORBIDDEN, "not_owner", "Only the seller who owns this listing can change it.");
    }

    public static ApiException locked() {
        return new ApiException(HttpStatus.CONFLICT, "listing_locked",
                "This listing is reserved or sold and can no longer be changed.");
    }
}
