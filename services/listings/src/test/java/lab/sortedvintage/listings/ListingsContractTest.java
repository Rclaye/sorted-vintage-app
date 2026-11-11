package lab.sortedvintage.listings;

import static org.assertj.core.api.Assertions.assertThat;

import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.function.Consumer;
import javax.imageio.ImageIO;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import lab.sortedvintage.listings.photo.PendingPhotoCleanup;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.boot.testcontainers.service.connection.ServiceConnection;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.postgresql.PostgreSQLContainer;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.S3Object;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.MissingNode;

/**
 * T033: every Listings route in contracts/listings.openapi.yaml except reservations (Sprint 3),
 * against a real server, Postgres and Garage. Field rules come from data-model.md and the error
 * body from contracts/README.md: {"error": "<code>", "message": "<text>"}.
 *
 * A real server on a random port, not MockMvc, because the 413 check needs the servlet
 * container's multipart limits. Payments' payout-status route is a local stub.
 *
 * Configuration the service reads (application.yaml maps the lab's S3_* and PAYMENTS_URL
 * environment variables onto these): listings.s3.endpoint, region, bucket, access-key-id,
 * secret-access-key, and listings.payments.url.
 *
 * Each test uses its own sellers and a brand of its own, so tests share one database without
 * seeing each other's listings.
 *
 * Publishing goes through the real route (T046), which asks the Payments stub with a
 * client-credentials token from TestTokens' Keycloak stand-in.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "spring.security.oauth2.resourceserver.jwt.issuer-uri=" + TestTokens.ISSUER,
        "spring.security.oauth2.resourceserver.jwt.audiences=listings"
})
@Testcontainers(disabledWithoutDocker = true)
class ListingsContractTest {

    static final int MAX_PHOTO_BYTES = 10_485_760;

    @Container
    @ServiceConnection
    static final PostgreSQLContainer POSTGRES = new PostgreSQLContainer("postgres:17-alpine");

    @Container
    static final GarageContainer GARAGE = new GarageContainer();

    static final TestTokens TOKENS = new TestTokens();
    static final PaymentsStub PAYMENTS = new PaymentsStub();
    static final JsonMapper JSON = JsonMapper.builder().build();
    static S3Client s3;

    @DynamicPropertySource
    static void services(DynamicPropertyRegistry registry) {
        registry.add("spring.security.oauth2.resourceserver.jwt.jwk-set-uri", TOKENS::jwksUri);
        registry.add("listings.s3.endpoint", GARAGE::s3Endpoint);
        registry.add("listings.s3.region", () -> GarageContainer.REGION);
        registry.add("listings.s3.bucket", () -> GarageContainer.BUCKET);
        registry.add("listings.s3.access-key-id", GARAGE::accessKeyId);
        registry.add("listings.s3.secret-access-key", GARAGE::secretAccessKey);
        registry.add("listings.payments.url", PAYMENTS::url);
        registry.add("listings.keycloak.token-url", TOKENS::tokenUri);
        registry.add("listings.keycloak.client-secret", () -> "listings-test-secret");
    }

    @BeforeAll
    static void s3Client() {
        s3 = S3Client.builder()
                .endpointOverride(URI.create(GARAGE.s3Endpoint()))
                .region(Region.of(GarageContainer.REGION))
                .forcePathStyle(true)
                .credentialsProvider(StaticCredentialsProvider.create(
                        AwsBasicCredentials.create(GARAGE.accessKeyId(), GARAGE.secretAccessKey())))
                .build();
    }

    @AfterAll
    static void closeS3() {
        if (s3 != null) {
            s3.close();
        }
    }

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    PendingPhotoCleanup cleanup;

    // ---------------------------------------------------------------- POST /listings

    @Nested
    class CreateDraft {

        @Test
        void createsADraftOwnedByTheCaller() {
            Seller seller = newSeller("Sam");
            Map<String, Object> input = input(brand());

            Resp created = send(HttpMethod.POST, "/listings", seller.token(), input);

            assertThat(created.status()).isEqualTo(201);
            JsonNode listing = created.body();
            assertThat(listing.path("id").asString()).isNotBlank();
            assertThat(listing.path("seller_sub").asString()).isEqualTo(seller.sub().toString());
            assertThat(listing.path("seller_display_name").asString()).isEqualTo("Sam");
            assertThat(listing.path("status").asString()).isEqualTo("draft");
            assertThat(listing.path("version").isInt()).isTrue();
            assertThat(listing.path("photos").isArray()).isTrue();
            assertThat(listing.path("photos").size()).isZero();
            assertThat(listing.path("published_at").isNull() || listing.path("published_at").isMissingNode())
                    .isTrue();
            assertThat(listing.path("title").asString()).isEqualTo(input.get("title"));
            assertThat(listing.path("brand").asString()).isEqualTo(input.get("brand"));
            assertThat(listing.path("category").asString()).isEqualTo("outerwear");
            assertThat(listing.path("size").asString()).isEqualTo("M");
            assertThat(listing.path("condition").asString()).isEqualTo("good");
            assertThat(listing.path("price_cents").asInt()).isEqualTo(4500);
        }

        @Test
        void needsAToken() {
            assertError(send(HttpMethod.POST, "/listings", null, input(brand())), 401, "invalid_token");
        }

        @Test
        void rejectsInvalidInputWith422() {
            Seller seller = newSeller("Sam");
            List<Consumer<Map<String, Object>>> breakages = List.of(
                    in -> in.put("title", "ab"),
                    in -> in.put("title", "x".repeat(81)),
                    in -> in.put("brand", ""),
                    in -> in.put("category", "hats"),
                    in -> in.put("size", "x".repeat(11)),
                    in -> in.put("condition", "worn"),
                    in -> in.put("price_cents", 99),
                    in -> in.put("price_cents", 1_000_001),
                    in -> in.put("description", "x".repeat(2001)),
                    in -> in.remove("title"),
                    in -> in.remove("price_cents"));
            for (Consumer<Map<String, Object>> breakage : breakages) {
                Map<String, Object> in = input(brand());
                breakage.accept(in);
                Resp resp = send(HttpMethod.POST, "/listings", seller.token(), in);
                assertThat(resp.status()).as("input %s", in).isEqualTo(422);
                assertThat(resp.body().path("error").asString()).as("input %s", in).isNotBlank();
                assertThat(resp.body().path("message").asString()).as("input %s", in).isNotBlank();
            }
        }

        @Test
        void createsTheSellerProfileOnTheFirstDraft() {
            Seller seller = newSeller("Robin");
            assertThat(send(HttpMethod.GET, "/sellers/" + seller.sub(), null, null).status()).isEqualTo(404);

            createDraft(seller, input(brand()));

            Resp profile = send(HttpMethod.GET, "/sellers/" + seller.sub(), null, null);
            assertThat(profile.status()).isEqualTo(200);
            assertThat(profile.body().path("display_name").asString()).isEqualTo("Robin");
        }
    }

    // ---------------------------------------------------------------- GET /listings/{id}

    @Nested
    class GetListing {

        @Test
        void anyoneCanReadAnAvailableListing() {
            Seller seller = newSeller("Sam");
            JsonNode published = publishWithPhoto(seller, input(brand()));

            Resp resp = send(HttpMethod.GET, "/listings/" + id(published), null, null);

            assertThat(resp.status()).isEqualTo(200);
            assertThat(resp.body().path("id").asString()).isEqualTo(id(published));
            assertThat(resp.body().path("status").asString()).isEqualTo("available");
            assertThat(resp.body().path("photos").size()).isEqualTo(1);
        }

        @Test
        void theOwnerCanReadTheirDraft() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));

            Resp resp = send(HttpMethod.GET, "/listings/" + id(draft), seller.token(), null);

            assertThat(resp.status()).isEqualTo(200);
            assertThat(resp.body().path("status").asString()).isEqualTo("draft");
        }

        @Test
        void unknownIdIs404() {
            assertError(send(HttpMethod.GET, "/listings/" + UUID.randomUUID(), null, null), 404, null);
        }
    }

    // ---------------------------------------------------------------- PATCH /listings/{id}

    @Nested
    class EditListing {

        @Test
        void theOwnerEditsWithTheCurrentVersion() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            Map<String, Object> changed = input(draft.path("brand").asString());
            changed.put("price_cents", 3900);
            changed.put("title", "Corduroy jacket, mended");

            Resp resp = patch(draft, seller.token(), version(draft), changed);

            assertThat(resp.status()).isEqualTo(200);
            assertThat(resp.body().path("price_cents").asInt()).isEqualTo(3900);
            assertThat(resp.body().path("title").asString()).isEqualTo("Corduroy jacket, mended");
            assertThat(resp.body().path("version").asInt()).isNotEqualTo(version(draft));
        }

        @Test
        void aStaleVersionIs409VersionConflict() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            Map<String, Object> changed = input(draft.path("brand").asString());
            assertThat(patch(draft, seller.token(), version(draft), changed).status()).isEqualTo(200);

            changed.put("price_cents", 5000);
            assertError(patch(draft, seller.token(), version(draft), changed), 409, "version_conflict");
        }

        @Test
        void anotherSellerIs403() {
            JsonNode draft = createDraft(newSeller("Sam"), input(brand()));
            Resp resp = patch(draft, newSeller("Alex").token(), version(draft), input(brand()));
            assertError(resp, 403, null);
        }

        @Test
        void reservedOrSoldIs409ListingLocked() {
            for (String locked : List.of("reserved", "sold")) {
                Seller seller = newSeller("Sam");
                JsonNode listing = publishWithPhoto(seller, input(brand()));
                setStatus(listing, locked);
                JsonNode current = get(listing, seller.token());

                Resp resp = patch(current, seller.token(), version(current), input(listing.path("brand").asString()));
                assertError(resp, 409, "listing_locked");
            }
        }

        @Test
        void needsAToken() {
            JsonNode draft = createDraft(newSeller("Sam"), input(brand()));
            assertError(patch(draft, null, version(draft), input(brand())), 401, "invalid_token");
        }

        @Test
        void copiesTheDisplayNameFromTheTokenOnEachWrite() {
            UUID sub = UUID.randomUUID();
            JsonNode draft = createDraft(new Seller(sub, "Sam"), input(brand()));

            Resp resp = patch(draft, new Seller(sub, "Sam V.").token(), version(draft),
                    input(draft.path("brand").asString()));

            assertThat(resp.status()).isEqualTo(200);
            assertThat(resp.body().path("seller_display_name").asString()).isEqualTo("Sam V.");
            assertThat(send(HttpMethod.GET, "/sellers/" + sub, null, null).body().path("display_name").asString())
                    .isEqualTo("Sam V.");
        }
    }

    // ---------------------------------------------------------------- DELETE /listings/{id}

    @Nested
    class DeleteListing {

        @Test
        void theOwnerDeletesAndItIsGone() {
            Seller seller = newSeller("Sam");
            JsonNode listing = publishWithPhoto(seller, input(brand()));

            assertThat(send(HttpMethod.DELETE, "/listings/" + id(listing), seller.token(), null).status())
                    .isEqualTo(204);

            assertThat(send(HttpMethod.GET, "/listings/" + id(listing), null, null).status()).isEqualTo(404);
            assertThat(browse("brand=" + listing.path("brand").asString()).path("items").size()).isZero();
        }

        @Test
        void anotherSellerIs403() {
            JsonNode draft = createDraft(newSeller("Sam"), input(brand()));
            assertError(send(HttpMethod.DELETE, "/listings/" + id(draft), newSeller("Alex").token(), null), 403, null);
        }

        @Test
        void reservedOrSoldIs409ListingLocked() {
            for (String locked : List.of("reserved", "sold")) {
                Seller seller = newSeller("Sam");
                JsonNode listing = publishWithPhoto(seller, input(brand()));
                setStatus(listing, locked);

                assertError(send(HttpMethod.DELETE, "/listings/" + id(listing), seller.token(), null),
                        409, "listing_locked");
            }
        }
    }

    // ---------------------------------------------------------------- POST /listings/{id}/photos

    @Nested
    class UploadPhoto {

        @Test
        void storesAJpegInGarage() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));

            Resp resp = upload(draft, seller.token(), jpeg(), "front.jpg", MediaType.IMAGE_JPEG_VALUE, null);

            assertThat(resp.status()).isEqualTo(201);
            String photoId = resp.body().path("id").asString();
            assertThat(photoId).isNotBlank();
            assertThat(resp.body().path("content_type").asString()).isEqualTo("image/jpeg");
            assertThat(resp.body().path("url").asString()).isNotBlank();
            assertThat(resp.body().path("position").isInt()).isTrue();
            assertThat(objectKeys(draft)).containsExactly("listings/" + id(draft) + "/" + photoId);
            assertThat(jdbc.queryForObject("select state from photo where id = ?::uuid", String.class, photoId))
                    .isEqualTo("stored");
            assertThat(get(draft, seller.token()).path("photos").size()).isEqualTo(1);
        }

        @Test
        void detectsTheTypeFromTheBytesNotTheNameOrHeader() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));

            Resp png = upload(draft, seller.token(), png(), "looks.jpg", MediaType.IMAGE_JPEG_VALUE, null);
            Resp webp = upload(draft, seller.token(), webp(), "photo.bin", MediaType.APPLICATION_OCTET_STREAM_VALUE,
                    null);

            assertThat(png.status()).isEqualTo(201);
            assertThat(png.body().path("content_type").asString()).isEqualTo("image/png");
            assertThat(webp.status()).isEqualTo(201);
            assertThat(webp.body().path("content_type").asString()).isEqualTo("image/webp");
        }

        @Test
        void textRenamedToJpgIs415AndLeavesNothingBehind() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            byte[] text = "just some text, not a picture\n".repeat(50).getBytes(StandardCharsets.UTF_8);

            Resp resp = upload(draft, seller.token(), text, "photo.jpg", MediaType.IMAGE_JPEG_VALUE, null);

            assertError(resp, 415, "photo_type_rejected");
            assertThat(objectKeys(draft)).isEmpty();
            assertThat(photoRows(draft)).isZero();
        }

        @Test
        void elevenMegabytesIs413AndLeavesNothingBehind() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            byte[] big = Arrays.copyOf(jpeg(), 11 * 1024 * 1024);

            Resp resp = upload(draft, seller.token(), big, "big.jpg", MediaType.IMAGE_JPEG_VALUE, null);

            assertError(resp, 413, "photo_too_large");
            assertThat(objectKeys(draft)).isEmpty();
            assertThat(photoRows(draft)).isZero();
        }

        @Test
        void exactlyTenMegabytesIsAccepted() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            byte[] limit = Arrays.copyOf(jpeg(), MAX_PHOTO_BYTES);

            assertThat(upload(draft, seller.token(), limit, "limit.jpg", MediaType.IMAGE_JPEG_VALUE, null).status())
                    .isEqualTo(201);
        }

        @Test
        void eightPhotosThenTheNinthIs409PhotoLimit() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            Set<Integer> positions = new HashSet<>();
            for (int i = 0; i < 8; i++) {
                Resp resp = upload(draft, seller.token(), jpeg(), "p" + i + ".jpg", MediaType.IMAGE_JPEG_VALUE, null);
                assertThat(resp.status()).as("photo %d", i + 1).isEqualTo(201);
                positions.add(resp.body().path("position").asInt());
            }
            assertThat(positions).containsExactlyInAnyOrder(0, 1, 2, 3, 4, 5, 6, 7);

            Resp ninth = upload(draft, seller.token(), jpeg(), "p8.jpg", MediaType.IMAGE_JPEG_VALUE, null);

            assertError(ninth, 409, "photo_limit");
            assertThat(objectKeys(draft)).hasSize(8);
        }

        @Test
        void takesTheRequestedPosition() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));

            Resp resp = upload(draft, seller.token(), jpeg(), "back.jpg", MediaType.IMAGE_JPEG_VALUE, 3);

            assertThat(resp.status()).isEqualTo(201);
            assertThat(resp.body().path("position").asInt()).isEqualTo(3);
        }

        @Test
        void anotherSellerIs403() {
            JsonNode draft = createDraft(newSeller("Sam"), input(brand()));
            Resp resp = upload(draft, newSeller("Alex").token(), jpeg(), "x.jpg", MediaType.IMAGE_JPEG_VALUE, null);
            assertError(resp, 403, null);
            assertThat(objectKeys(draft)).isEmpty();
        }

        @Test
        void needsAToken() {
            JsonNode draft = createDraft(newSeller("Sam"), input(brand()));
            assertError(upload(draft, null, jpeg(), "x.jpg", MediaType.IMAGE_JPEG_VALUE, null), 401, "invalid_token");
        }
    }

    // ---------------------------------------------------------------- DELETE /listings/{id}/photos/{photo_id}

    @Nested
    class RemovePhoto {

        @Test
        void theOwnerRemovesAPhotoAndItsObject() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            String photoId = upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null)
                    .body().path("id").asString();

            Resp resp = send(HttpMethod.DELETE, "/listings/" + id(draft) + "/photos/" + photoId, seller.token(), null);

            assertThat(resp.status()).isEqualTo(204);
            assertThat(objectKeys(draft)).isEmpty();
            assertThat(get(draft, seller.token()).path("photos").size()).isZero();
        }

        @Test
        void anotherSellerIs403() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            String photoId = upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null)
                    .body().path("id").asString();

            assertError(send(HttpMethod.DELETE, "/listings/" + id(draft) + "/photos/" + photoId,
                    newSeller("Alex").token(), null), 403, null);
            assertThat(objectKeys(draft)).hasSize(1);
        }

        @Test
        void notWhileReservedOrSold() {
            for (String locked : List.of("reserved", "sold")) {
                Seller seller = newSeller("Sam");
                JsonNode listing = publishWithPhoto(seller, input(brand()));
                String photoId = listing.path("photos").get(0).path("id").asString();
                setStatus(listing, locked);

                assertError(send(HttpMethod.DELETE, "/listings/" + id(listing) + "/photos/" + photoId,
                        seller.token(), null), 409, "listing_locked");
                assertThat(objectKeys(listing)).hasSize(1);
            }
        }
    }

    // ---------------------------------------------------------------- POST /listings/{id}/publish

    @Nested
    class Publish {

        @Test
        void publishingTwiceIs409NotADraft() {
            Seller seller = newSeller("Sam");
            JsonNode listing = publishWithPhoto(seller, input(brand()));

            assertError(send(HttpMethod.POST, "/listings/" + id(listing) + "/publish", seller.token(), null),
                    409, "not_a_draft");
        }

        @Test
        void paymentsUnavailableIs503AndTheListingStaysADraft() {
            Seller seller = newSeller("Sam");
            PAYMENTS.payoutStatus(seller.sub(), "error");
            JsonNode draft = createDraft(seller, input(brand()));
            upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null);

            assertError(send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null),
                    503, "payout_status_unavailable");
            assertThat(get(draft, seller.token()).path("status").asString()).isEqualTo("draft");
        }

        @Test
        void listingsCachesItsServiceToken() {
            int before = TOKENS.tokenRequests();
            for (int i = 0; i < 3; i++) {
                publishWithPhoto(newSeller("Sam"), input(brand()));
            }
            assertThat(TOKENS.tokenRequests() - before).as("one token covers several publishes").isLessThanOrEqualTo(1);
        }

        @Test
        void aDraftWithAPhotoAndPayoutsReadyBecomesAvailable() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null);
            int callsBefore = PAYMENTS.authorizations().size();

            Resp resp = send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null);

            assertThat(resp.status()).isEqualTo(200);
            assertThat(resp.body().path("status").asString()).isEqualTo("available");
            assertThat(resp.body().path("published_at").asString()).isNotBlank();
            assertThat(PAYMENTS.authorizations()).as("Listings asks Payments for the payout status")
                    .hasSizeGreaterThan(callsBefore);
            assertThat(PAYMENTS.authorizations().getLast()).startsWith("Bearer ");
        }

        @Test
        void withoutPhotosIs409PhotosRequired() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));

            assertError(send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null),
                    409, "photos_required");
        }

        @Test
        void pendingPhotosDoNotCount() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            String photoId = upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null)
                    .body().path("id").asString();
            jdbc.update("update photo set state = 'pending' where id = ?::uuid", photoId);

            assertError(send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null),
                    409, "photos_required");
        }

        @Test
        void payoutsNotReadyIs409PayoutOnboardingRequired() {
            for (String status : List.of("none", "pending")) {
                Seller seller = newSeller("Sam");
                PAYMENTS.payoutStatus(seller.sub(), status);
                JsonNode draft = createDraft(seller, input(brand()));
                upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null);

                assertError(send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null),
                        409, "payout_onboarding_required");
                assertThat(get(draft, seller.token()).path("status").asString()).isEqualTo("draft");
            }
        }

        @Test
        void anotherSellerIs403() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null);

            assertError(send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", newSeller("Alex").token(), null),
                    403, null);
        }
    }

    // ---------------------------------------------------------------- GET /listings

    @Nested
    class BrowseAndSearch {

        @Test
        void showsOnlyAvailableListingsNewestFirstWithoutAToken() {
            Seller seller = newSeller("Sam");
            String brand = brand();
            JsonNode older = publishWithPhoto(seller, input(brand));
            JsonNode newer = publishWithPhoto(seller, input(brand));
            jdbc.update("update listing set published_at = now() - interval '1 hour' where id = ?::uuid", id(older));
            createDraft(seller, input(brand));
            JsonNode sold = publishWithPhoto(seller, input(brand));
            setStatus(sold, "sold");

            JsonNode page = browse("brand=" + brand);

            assertThat(ids(page)).containsExactly(id(newer), id(older));
            for (JsonNode item : page.path("items")) {
                assertThat(item.path("status").asString()).isEqualTo("available");
            }
        }

        @Test
        void searchesTitleBrandAndDescription() {
            Seller seller = newSeller("Sam");
            String brand = brand();
            Map<String, Object> velvet = input(brand);
            velvet.put("title", "Velvet blazer");
            JsonNode match = publishWithPhoto(seller, velvet);
            publishWithPhoto(seller, input(brand));

            assertThat(ids(browse("brand=" + brand + "&q=velvet"))).containsExactly(id(match));
            assertThat(ids(browse("q=" + brand))).hasSize(2);
        }

        @Test
        void filtersBySizeCategoryAndPrice() {
            Seller seller = newSeller("Sam");
            String brand = brand();
            Map<String, Object> small = input(brand);
            small.put("size", "S");
            small.put("category", "tops");
            small.put("price_cents", 1500);
            JsonNode cheapTop = publishWithPhoto(seller, small);
            JsonNode jacket = publishWithPhoto(seller, input(brand));

            assertThat(ids(browse("brand=" + brand + "&size=S"))).containsExactly(id(cheapTop));
            assertThat(ids(browse("brand=" + brand + "&category=outerwear"))).containsExactly(id(jacket));
            assertThat(ids(browse("brand=" + brand + "&max_price_cents=2000"))).containsExactly(id(cheapTop));
            assertThat(ids(browse("brand=" + brand + "&min_price_cents=2000"))).containsExactly(id(jacket));
        }

        @Test
        void pagesWithACursor() {
            Seller seller = newSeller("Sam");
            String brand = brand();
            List<String> published = new ArrayList<>();
            for (int i = 0; i < 3; i++) {
                published.add(id(publishWithPhoto(seller, input(brand))));
            }

            JsonNode first = browse("brand=" + brand + "&limit=2");
            assertThat(first.path("items").size()).isEqualTo(2);
            String cursor = first.path("next_cursor").asString();
            assertThat(cursor).isNotBlank();

            JsonNode second = browse("brand=" + brand + "&limit=2&cursor="
                    + java.net.URLEncoder.encode(cursor, StandardCharsets.UTF_8));
            assertThat(second.path("items").size()).isEqualTo(1);
            assertThat(second.path("next_cursor").isNull() || second.path("next_cursor").isMissingNode()).isTrue();

            List<String> all = new ArrayList<>(ids(first));
            all.addAll(ids(second));
            assertThat(all).containsExactlyInAnyOrderElementsOf(published);
        }
    }

    // ---------------------------------------------------------------- GET /sellers/{account_sub}

    @Nested
    class SellerProfile {

        @Test
        void showsAvailableAndSoldListingsButNotDrafts() {
            Seller seller = newSeller("Jo");
            JsonNode available = publishWithPhoto(seller, input(brand()));
            JsonNode sold = publishWithPhoto(seller, input(brand()));
            setStatus(sold, "sold");
            jdbc.update("update seller_profile set sold_count = 1 where account_sub = ?::uuid", seller.sub().toString());
            createDraft(seller, input(brand()));

            Resp resp = send(HttpMethod.GET, "/sellers/" + seller.sub(), null, null);

            assertThat(resp.status()).isEqualTo(200);
            JsonNode profile = resp.body();
            assertThat(profile.path("account_sub").asString()).isEqualTo(seller.sub().toString());
            assertThat(profile.path("display_name").asString()).isEqualTo("Jo");
            assertThat(profile.path("sold_count").asInt()).isEqualTo(1);
            assertThat(idsOf(profile.path("available"))).containsExactly(id(available));
            assertThat(idsOf(profile.path("sold"))).containsExactly(id(sold));
        }

        @Test
        void theSellerAlsoSeesTheirDraftsAndNobodyElseDoes() {
            Seller seller = newSeller("Jo");
            JsonNode published = publishWithPhoto(seller, input(brand()));
            JsonNode draft = createDraft(seller, input(brand()));

            JsonNode own = send(HttpMethod.GET, "/sellers/" + seller.sub(), seller.token(), null).body();
            JsonNode anonymous = send(HttpMethod.GET, "/sellers/" + seller.sub(), null, null).body();
            JsonNode other = send(HttpMethod.GET, "/sellers/" + seller.sub(), newSeller("Alex").token(), null).body();

            assertThat(idsOf(own.path("drafts"))).containsExactly(id(draft));
            assertThat(idsOf(own.path("available"))).containsExactly(id(published));
            assertThat(anonymous.has("drafts")).as("no drafts field for visitors").isFalse();
            assertThat(other.has("drafts")).as("no drafts field for other sellers").isFalse();
        }

        @Test
        void unknownSellerIs404() {
            assertError(send(HttpMethod.GET, "/sellers/" + UUID.randomUUID(), null, null), 404, null);
        }
    }

    // ---------------------------------------------------------------- GET /photos/listings/{id}/{photo}

    @Nested
    class PhotoFiles {

        @Test
        void aPublishedListingsPhotoIsPublicAndCacheable() {
            Seller seller = newSeller("Sam");
            byte[] png = png();
            JsonNode draft = createDraft(seller, input(brand()));
            String url = upload(draft, seller.token(), png, "p.png", MediaType.IMAGE_PNG_VALUE, null)
                    .body().path("url").asString();
            assertThat(send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null).status())
                    .isEqualTo(200);

            ResponseEntity<byte[]> photo = bytes(url, null);

            assertThat(photo.getStatusCode().value()).isEqualTo(200);
            assertThat(photo.getHeaders().getContentType()).hasToString("image/png");
            assertThat(photo.getHeaders().getCacheControl()).contains("public").contains("immutable");
            assertThat(photo.getBody()).isEqualTo(png);
        }

        @Test
        void aDraftsPhotoIsForItsSellerOnly() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            String url = upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null)
                    .body().path("url").asString();

            assertThat(bytes(url, null).getStatusCode().value()).isEqualTo(404);
            assertThat(bytes(url, newSeller("Alex").token()).getStatusCode().value()).isEqualTo(404);
            ResponseEntity<byte[]> own = bytes(url, seller.token());
            assertThat(own.getStatusCode().value()).isEqualTo(200);
            assertThat(own.getHeaders().getCacheControl()).contains("no-store");
        }

        @Test
        void anUnknownPhotoIs404() {
            assertThat(bytes("/photos/listings/" + UUID.randomUUID() + "/" + UUID.randomUUID(), null)
                    .getStatusCode().value()).isEqualTo(404);
        }

        private ResponseEntity<byte[]> bytes(String path, String token) {
            return RestClient.builder().baseUrl("http://localhost:" + port)
                    .defaultStatusHandler(status -> true, (request, response) -> { })
                    .build().get().uri(path)
                    .headers(h -> { if (token != null) { h.setBearerAuth(token); } })
                    .retrieve().toEntity(byte[].class);
        }
    }

    // ---------------------------------------------------------------- T039, not a route

    @Nested
    class PendingPhotoCleanupJob {

        @Test
        void removesPendingPhotosOlderThanAnHourAndTheirObjects() {
            Seller seller = newSeller("Sam");
            JsonNode draft = createDraft(seller, input(brand()));
            String old = photoId(upload(draft, seller.token(), jpeg(), "a.jpg", MediaType.IMAGE_JPEG_VALUE, null));
            String recent = photoId(upload(draft, seller.token(), jpeg(), "b.jpg", MediaType.IMAGE_JPEG_VALUE, null));
            String stored = photoId(upload(draft, seller.token(), jpeg(), "c.jpg", MediaType.IMAGE_JPEG_VALUE, null));
            jdbc.update("update photo set state = 'pending', created_at = now() - interval '2 hours' where id = ?::uuid",
                    old);
            jdbc.update("update photo set state = 'pending', created_at = now() - interval '10 minutes' where id = ?::uuid",
                    recent);
            jdbc.update("update photo set created_at = now() - interval '2 hours' where id = ?::uuid", stored);

            assertThat(cleanup.run()).isGreaterThanOrEqualTo(1);

            assertThat(jdbc.queryForList("select id::text from photo where listing_id = ?::uuid", String.class, id(draft)))
                    .containsExactlyInAnyOrder(recent, stored);
            assertThat(objectKeys(draft)).containsExactlyInAnyOrder(
                    "listings/" + id(draft) + "/" + recent, "listings/" + id(draft) + "/" + stored);
        }
    }

    // ================================================================ helpers

    static String photoId(Resp upload) {
        assertThat(upload.status()).as("upload: %s", upload.raw()).isEqualTo(201);
        return upload.body().path("id").asString();
    }

    record Seller(UUID sub, String displayName) {
        String token() {
            return TOKENS.seller(sub, displayName);
        }
    }

    record Resp(int status, JsonNode body, String raw) {
    }

    static Seller newSeller(String displayName) {
        return new Seller(UUID.randomUUID(), displayName);
    }

    /** A brand no other test uses, so browse filters see only this test's listings. */
    static String brand() {
        return "b" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);
    }

    static Map<String, Object> input(String brand) {
        Map<String, Object> in = new HashMap<>();
        in.put("title", "Corduroy jacket");
        in.put("brand", brand);
        in.put("category", "outerwear");
        in.put("size", "M");
        in.put("condition", "good");
        in.put("price_cents", 4500);
        in.put("description", "Warm and sturdy, from the seventies.");
        return in;
    }

    JsonNode createDraft(Seller seller, Map<String, Object> input) {
        Resp resp = send(HttpMethod.POST, "/listings", seller.token(), input);
        assertThat(resp.status()).as("create draft: %s", resp.raw()).isEqualTo(201);
        return resp.body();
    }

    JsonNode publishWithPhoto(Seller seller, Map<String, Object> input) {
        JsonNode draft = createDraft(seller, input);
        Resp photo = upload(draft, seller.token(), jpeg(), "front.jpg", MediaType.IMAGE_JPEG_VALUE, null);
        assertThat(photo.status()).as("upload: %s", photo.raw()).isEqualTo(201);
        Resp published = send(HttpMethod.POST, "/listings/" + id(draft) + "/publish", seller.token(), null);
        assertThat(published.status()).as("publish: %s", published.raw()).isEqualTo(200);
        return published.body();
    }

    JsonNode get(JsonNode listing, String token) {
        Resp resp = send(HttpMethod.GET, "/listings/" + id(listing), token, null);
        assertThat(resp.status()).as("get: %s", resp.raw()).isEqualTo(200);
        return resp.body();
    }

    JsonNode browse(String query) {
        Resp resp = send(HttpMethod.GET, "/listings?" + query, null, null);
        assertThat(resp.status()).as("browse %s: %s", query, resp.raw()).isEqualTo(200);
        assertThat(resp.body().path("items").isArray()).isTrue();
        return resp.body();
    }

    Resp patch(JsonNode listing, String token, int version, Map<String, Object> input) {
        return exchange(HttpMethod.PATCH, "/listings/" + id(listing), token,
                headers -> headers.set(HttpHeaders.IF_MATCH, String.valueOf(version)),
                MediaType.APPLICATION_JSON, input);
    }

    Resp send(HttpMethod method, String path, String token, Object body) {
        return exchange(method, path, token, headers -> { }, MediaType.APPLICATION_JSON, body);
    }

    Resp upload(JsonNode listing, String token, byte[] bytes, String filename, String declaredType, Integer position) {
        MultipartBodyBuilder parts = new MultipartBodyBuilder();
        parts.part("file", new ByteArrayResource(bytes) {
            @Override
            public String getFilename() {
                return filename;
            }
        }).contentType(MediaType.parseMediaType(declaredType));
        if (position != null) {
            parts.part("position", String.valueOf(position));
        }
        return exchange(HttpMethod.POST, "/listings/" + id(listing) + "/photos", token, headers -> { },
                MediaType.MULTIPART_FORM_DATA, parts.build());
    }

    Resp exchange(HttpMethod method, String path, String token, Consumer<HttpHeaders> extra, MediaType type,
            Object body) {
        RestClient client = RestClient.builder()
                .baseUrl("http://localhost:" + port)
                .requestFactory(new JdkClientHttpRequestFactory(
                        java.net.http.HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build()))
                .defaultStatusHandler(status -> true, (request, response) -> { })
                .build();
        try {
            RestClient.RequestBodySpec spec = client.method(method).uri(path).headers(headers -> {
                if (token != null) {
                    headers.setBearerAuth(token);
                }
                extra.accept(headers);
            });
            if (body != null) {
                spec.contentType(type).body(body);
            }
            ResponseEntity<String> response = spec.retrieve().toEntity(String.class);
            String raw = response.getBody() == null ? "" : response.getBody();
            JsonNode json = raw.isBlank() ? MissingNode.getInstance() : parse(raw);
            return new Resp(response.getStatusCode().value(), json, raw);
        } catch (RestClientException e) {
            // For example the server closing the connection mid-upload instead of answering 413.
            return new Resp(-1, MissingNode.getInstance(), e.toString());
        }
    }

    static JsonNode parse(String raw) {
        try {
            return JSON.readTree(raw);
        } catch (RuntimeException e) {
            return MissingNode.getInstance();
        }
    }

    /** Status, and the contract's error body; {@code code} null means any machine code. */
    static void assertError(Resp resp, int status, String code) {
        assertThat(resp.status()).as("status, body %s", resp.raw()).isEqualTo(status);
        if (code != null) {
            assertThat(resp.body().path("error").asString()).as("error, body %s", resp.raw()).isEqualTo(code);
        } else {
            assertThat(resp.body().path("error").asString()).as("error, body %s", resp.raw()).isNotBlank();
        }
        assertThat(resp.body().path("message").asString()).as("message, body %s", resp.raw()).isNotBlank();
    }

    void setStatus(JsonNode listing, String status) {
        jdbc.update("update listing set status = ?, version = version + 1 where id = ?::uuid", status, id(listing));
    }

    int photoRows(JsonNode listing) {
        return jdbc.queryForObject("select count(*) from photo where listing_id = ?::uuid", Integer.class, id(listing));
    }

    static List<String> objectKeys(JsonNode listing) {
        return s3.listObjectsV2(r -> r.bucket(GarageContainer.BUCKET).prefix("listings/" + id(listing) + "/"))
                .contents().stream().map(S3Object::key).toList();
    }

    static String id(JsonNode listing) {
        return listing.path("id").asString();
    }

    static int version(JsonNode listing) {
        return listing.path("version").asInt();
    }

    static List<String> ids(JsonNode page) {
        return idsOf(page.path("items"));
    }

    static List<String> idsOf(JsonNode array) {
        List<String> ids = new ArrayList<>();
        array.forEach(item -> ids.add(item.path("id").asString()));
        return ids;
    }

    // ---------------------------------------------------------------- images

    static byte[] jpeg() {
        return image("jpg");
    }

    static byte[] png() {
        return image("png");
    }

    /** A 1x1 lossless WebP; ImageIO cannot write WebP. */
    static byte[] webp() {
        return Base64.getDecoder().decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==");
    }

    private static byte[] image(String format) {
        try {
            BufferedImage image = new BufferedImage(8, 8, BufferedImage.TYPE_INT_RGB);
            image.setRGB(3, 3, 0x8B4513);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            assertThat(ImageIO.write(image, format, out)).isTrue();
            return out.toByteArray();
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }
}
