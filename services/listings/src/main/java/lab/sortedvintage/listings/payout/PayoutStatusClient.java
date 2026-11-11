package lab.sortedvintage.listings.payout;

import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Map;
import java.util.UUID;
import lab.sortedvintage.listings.web.ApiException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/**
 * Asks Payments whether a seller can be paid (T046, FR-014), with Listings' own client-credentials
 * token from Keycloak (client {@code listings}, whose tokens carry the payments audience). The
 * token is cached until 30 seconds before it expires.
 */
@Component
public class PayoutStatusClient {

    private static final Logger log = LoggerFactory.getLogger(PayoutStatusClient.class);
    private static final Duration EARLY = Duration.ofSeconds(30);

    private final RestClient http;
    private final String paymentsUrl;
    private final String tokenUrl;
    private final String basicAuth;
    private final Clock clock;

    private String token;
    private Instant tokenExpires = Instant.EPOCH;

    public PayoutStatusClient(@Value("${listings.payments.url}") String paymentsUrl,
            @Value("${listings.keycloak.token-url}") String tokenUrl,
            @Value("${listings.keycloak.client-id}") String clientId,
            @Value("${listings.keycloak.client-secret}") String clientSecret) {
        this.http = RestClient.builder()
                .requestFactory(new JdkClientHttpRequestFactory(
                        HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build()))
                .build();
        this.paymentsUrl = paymentsUrl.replaceAll("/+$", "");
        this.tokenUrl = tokenUrl;
        this.basicAuth = "Basic " + Base64.getEncoder()
                .encodeToString((clientId + ":" + clientSecret).getBytes(StandardCharsets.UTF_8));
        this.clock = Clock.systemUTC();
    }

    /** none, pending or ready (contracts/payments.openapi.yaml). */
    public String payoutStatus(UUID sellerSub) {
        try {
            Map<?, ?> body = http.get()
                    .uri(paymentsUrl + "/connected-accounts/{sub}/payout-status", sellerSub)
                    .header(HttpHeaders.AUTHORIZATION, "Bearer " + token())
                    .retrieve()
                    .body(Map.class);
            Object status = body == null ? null : body.get("payout_status");
            if (status == null) {
                throw new IllegalStateException("no payout_status in the answer");
            }
            return status.toString();
        } catch (RestClientException | IllegalStateException e) {
            log.warn("payout status unavailable for {}: {}", sellerSub, e.toString());
            throw new ApiException(HttpStatus.SERVICE_UNAVAILABLE, "payout_status_unavailable",
                    "Payout status cannot be checked right now. Try again in a moment.");
        }
    }

    private synchronized String token() {
        Instant now = clock.instant();
        if (token != null && now.isBefore(tokenExpires.minus(EARLY))) {
            return token;
        }
        Map<?, ?> answer = http.post()
                .uri(tokenUrl)
                .header(HttpHeaders.AUTHORIZATION, basicAuth)
                .contentType(MediaType.APPLICATION_FORM_URLENCODED)
                .body("grant_type=client_credentials")
                .retrieve()
                .body(Map.class);
        if (answer == null || answer.get("access_token") == null) {
            throw new IllegalStateException("no access_token from Keycloak");
        }
        token = answer.get("access_token").toString();
        Object expiresIn = answer.get("expires_in");
        tokenExpires = now.plusSeconds(expiresIn instanceof Number n ? n.longValue() : 60);
        return token;
    }
}
