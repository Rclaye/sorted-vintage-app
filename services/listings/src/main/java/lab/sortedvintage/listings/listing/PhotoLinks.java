package lab.sortedvintage.listings.listing;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * Turns a photo's storage key into the URL a browser loads. Garage is only reachable inside the
 * cluster, so a presigned URL would not work in a browser; the contract also allows a proxied URL,
 * and this is one: a path under {@code listings.photos.url-prefix} (default {@code /photos/}),
 * which the BFF is to serve from Garage (T048).
 */
@Component
public class PhotoLinks {

    private final String prefix;

    public PhotoLinks(@Value("${listings.photos.url-prefix:/photos/}") String prefix) {
        this.prefix = prefix.endsWith("/") ? prefix : prefix + "/";
    }

    public String url(String storageKey) {
        return prefix + storageKey;
    }
}
