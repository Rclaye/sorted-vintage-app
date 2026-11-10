package lab.sortedvintage.listings.config;

import java.net.URI;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.checksums.RequestChecksumCalculation;
import software.amazon.awssdk.core.checksums.ResponseChecksumValidation;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;

/**
 * The S3 client for Garage (research R19), from listings.s3.* (application.yaml maps the lab's
 * S3_* variables onto them). Path-style addressing, because Garage serves buckets under the path
 * rather than as subdomains. Checksums only when an operation requires them: the SDK's default
 * streaming checksums are an AWS extension that other S3 servers may not accept.
 */
@Configuration
public class S3Config {

    @Bean(destroyMethod = "close")
    S3Client s3(@Value("${listings.s3.endpoint}") String endpoint,
            @Value("${listings.s3.region}") String region,
            @Value("${listings.s3.access-key-id}") String accessKeyId,
            @Value("${listings.s3.secret-access-key}") String secretAccessKey) {
        return S3Client.builder()
                .endpointOverride(URI.create(endpoint))
                .region(Region.of(region))
                .forcePathStyle(true)
                .credentialsProvider(StaticCredentialsProvider.create(
                        AwsBasicCredentials.create(accessKeyId, secretAccessKey)))
                .requestChecksumCalculation(RequestChecksumCalculation.WHEN_REQUIRED)
                .responseChecksumValidation(ResponseChecksumValidation.WHEN_REQUIRED)
                .build();
    }
}
