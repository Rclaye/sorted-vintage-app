// T073: the Payment Element on the pay page. Loaded after Stripe.js (both deferred, in order).
// The client secret comes from the page's data attributes and is never sent anywhere but Stripe.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("payment-form");
  if (!form) return;
  const pay = document.getElementById("pay");
  const message = document.getElementById("payment-message");
  const show = (text) => {
    message.textContent = text;
    message.hidden = false;
  };
  if (typeof Stripe === "undefined") {
    show("The payment form could not load. Reload the page to try again.");
    return;
  }
  const stripe = Stripe(form.dataset.publishableKey);
  const elements = stripe.elements({ clientSecret: form.dataset.clientSecret });
  const element = elements.create("payment");
  element.mount("#payment-element");
  element.on("ready", () => {
    pay.disabled = false;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    pay.disabled = true;
    message.hidden = true;
    const { error } = await stripe.confirmPayment({
      elements,
      confirmParams: { return_url: form.dataset.returnUrl },
    });
    // Reached only when the payment was not confirmed; otherwise Stripe goes to return_url.
    if (error.type === "validation_error") {
      show(error.message);
      pay.disabled = false;
      return;
    }
    if (error.type === "card_error") {
      // A declined payment ends this order (the item goes back on sale): start again from the listing.
      show(`${error.message} Nothing was charged.`);
      const link = document.createElement("a");
      link.href = form.dataset.orderUrl;
      link.textContent = " See the order.";
      message.append(link);
      return;
    }
    show("Something went wrong on the way to Stripe. Try again in a moment.");
    pay.disabled = false;
  });
});
