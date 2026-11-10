package lab.sortedvintage.listings;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
public class ListingsApplication {

    public static void main(String[] args) {
        SpringApplication.run(ListingsApplication.class, args);
    }
}
