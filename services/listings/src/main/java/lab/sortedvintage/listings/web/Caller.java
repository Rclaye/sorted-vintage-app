package lab.sortedvintage.listings.web;

import java.util.UUID;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;

/**
 * The account behind a request: its Keycloak subject and the display_name claim the realm issues
 * (gitops platform/keycloak/realms). Anonymous callers have none.
 */
public record Caller(UUID sub, String displayName) {

    static final int MAX_DISPLAY_NAME = 40;

    /** The signed-in caller, or null for an anonymous request. */
    public static Caller of(Authentication auth) {
        if (!(auth instanceof JwtAuthenticationToken jwt)) {
            return null;
        }
        UUID sub;
        try {
            sub = UUID.fromString(jwt.getToken().getSubject());
        } catch (RuntimeException e) {
            throw new ApiException(org.springframework.http.HttpStatus.FORBIDDEN, "invalid_subject",
                    "The token's subject is not an account id.");
        }
        String name = firstNonBlank(jwt.getToken().getClaimAsString("display_name"),
                jwt.getToken().getClaimAsString("preferred_username"), "Seller");
        if (name.length() > MAX_DISPLAY_NAME) {
            name = name.substring(0, MAX_DISPLAY_NAME);
        }
        return new Caller(sub, name);
    }

    /** The signed-in caller; routes that call this are behind authentication already. */
    public static Caller required(Authentication auth) {
        Caller caller = of(auth);
        if (caller == null) {
            throw new ApiException(org.springframework.http.HttpStatus.UNAUTHORIZED, "invalid_token",
                    "A bearer token is required.");
        }
        return caller;
    }

    private static String firstNonBlank(String... values) {
        for (String v : values) {
            if (v != null && !v.isBlank()) {
                return v.strip();
            }
        }
        return null;
    }
}
