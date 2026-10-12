const link = (href: string, text: string) => <a href={href} target="_blank" rel="noopener noreferrer">{text}</a>;

function GuideScreenshot({ file, caption }: { file: string; caption: string }) {
  return <details style={{ margin: "12px 0" }}>
    <summary>Screenshot: {caption}</summary>
    <figure style={{ margin: "12px 0" }}>
      <img src={`${import.meta.env.BASE_URL}help/deepseek/${file}`} alt={caption} loading="lazy"
        style={{ display: "block", maxWidth: "100%", width: 320, height: "auto", borderRadius: 8 }} />
      <figcaption className="hint">{caption}. Captured October 12, 2026; layout, amounts and payment options may vary. Account identity is excluded.</figcaption>
    </figure>
  </details>;
}

export default function DeepSeekGuide() {
  return <details className="settingsGroup">
    <summary><b>How to get a DeepSeek API key and set up billing</b></summary>
    <p>You need your own DeepSeek API platform account. Keep your password, payment details and API key private; enter payment information only on DeepSeek’s website.</p>
    <ol>
      <li><b>Open the official platform.</b> Visit {link("https://platform.deepseek.com", "platform.deepseek.com")}. Verify the address before signing in. Use the sign-up option if you do not have an account, then complete the verification requested by DeepSeek.</li>
      <li><b>Set up API billing.</b> Open the menu (the three-line button on narrow screens), choose Usage, then Top up. You can also choose Top up directly in the menu. Choose a currency, an amount and a payment method, review the total including any tax, and complete payment yourself on DeepSeek’s website. Review {link("https://api-docs.deepseek.com/quick_start/pricing/", "current API pricing")} and the payment options available for your account. Add only the credit you intend to spend. If spending limits or alerts are offered, configure them there (Usage shows a Settings control for balance alerts); Codex Engine does not control your DeepSeek billing.
        <GuideScreenshot file="01-usage.jpg" caption="Usage: balance, alert Settings and Top up" />
        <GuideScreenshot file="02-top-up.jpg" caption="Top up: amount, total and payment methods" />
      </li>
      <li><b>Find API keys.</b> Open the menu and choose API keys, or use {link("https://platform.deepseek.com/api_keys", "this API keys link")} after signing in. Labels and layout may change.
        <GuideScreenshot file="03-navigation.jpg" caption="Menu: Usage, API keys, Top up and Billing" />
        <GuideScreenshot file="04-api-keys.jpg" caption="API keys: Create new API key" />
      </li>
      <li><b>Create a key.</b> Click Create new API key. Enter a recognizable name in Name of the API key, such as “Codex Engine — my PC”, then click Create API key when you are ready. Copy the newly generated key immediately and keep it in a password manager if needed: DeepSeek says it can be viewed and copied only once at creation. Keep it private.
        <GuideScreenshot file="05-create-key.jpg" caption="Create new API key: enter a name, then Create API key" />
      </li>
      <li><b>Add it here.</b> Choose DeepSeek under Formatting provider. Paste the key into the password field, with no extra spaces. Leave the enable checkbox off while setting up. Click Save. Saving the key alone does not enable DeepSeek.</li>
      <li><b>Check the key.</b> Click Test API key. This checks authentication and your API balance without sending any book text. If there is insufficient credit, return to DeepSeek’s billing section. Successful authentication does not guarantee a particular model is available.</li>
      <li><b>Enable when ready.</b> Read the billing and privacy warning, check “Enable DeepSeek”, make sure “Use AI to format pages” is checked, then Save. Open a page in the reader and use AI format. The app sends its cleaned text in sections, checks returned Markdown and keeps the original cleaned text for rejected sections. A failed or rejected generation can still cost money; the app may retry a section once.</li>
      <li><b>Disable or revoke later.</b> Uncheck Enable DeepSeek and Save to stop further requests. Already generated results remain available. To remove this computer’s key, check Remove the saved key and Save. To revoke it completely, delete/revoke it in the platform’s API keys section. To replace it, create a new key, paste it here and Save, then revoke the old one.</li>
    </ol>
    <p><b>Common problems:</b> An authentication error means the key may be wrong or revoked. An insufficient-balance error requires API credit. A rate-limit error means wait and try later. A connection error requires checking internet access and service availability. If a model is unavailable, check DeepSeek’s current documentation and update the model name. Formatting that alters the wording is rejected; try again, keep cleaned text or edit the page yourself.</p>
    <p>Free chat access and any promotional credit are not a promise of free API use. Requests, retries and regeneration use your account. Check usage and balance in the platform regularly. Cost confirmation is on by default: the app shows a dated pricing estimate before each paid formatting run, including retries and regeneration. These amounts are estimates, not a guaranteed bill or spending cap. Turn off the cost-confirmation option in Settings and Save if you prefer it to run without asking. Only page sections being formatted are sent; this feature does not upload the original PDF. Codex Engine includes a local model runtime. Download its model in Settings to format offline without API charges or installing separate software.</p>
  </details>;
}
