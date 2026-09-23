package com.fourindegree.vfswatch;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.webkit.ValueCallback;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * Reads VFS's public "earliest available date" endpoint.
 *
 * It has to go through a WebView: the endpoint sits behind Cloudflare, which checks the TLS
 * fingerprint of the client. A normal HttpURLConnection/OkHttp call is answered with 403205 even
 * with perfect headers, while Chromium (which WebView is) gets a 200. So we load the public VFS
 * page — that passes Cloudflare and sets its cookies — and then run the same in-page fetch() the
 * site itself uses.
 */
class Fetcher {

    interface Result {
        void ok(String json);
        void failed(String reason);
    }

    private static final String PAGE = "https://visa.vfsglobal.com/ind/en/bgr/";
    private static final String ENDPOINT = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot";

    private static final String JS =
        "(async () => { try {" +
        "  const r = await fetch('" + ENDPOINT + "', {method:'POST'," +
        "     headers:{'content-type':'application/json','accept':'application/json, text/plain, */*'," +
        "              'route':'ind/en/bgr'}," +
        "     body: JSON.stringify({missionCode:'bgr',countryCode:'ind',cultureCode:'en-US'})});" +
        "  return JSON.stringify({status:r.status, body: await r.text()});" +
        "} catch (e) { return JSON.stringify({status:0, body:String(e)}); } })()";

    private final Context ctx;
    private WebView web;
    private boolean done;

    Fetcher(Context ctx) { this.ctx = ctx; }

    /** Loads the page, waits for Cloudflare, then asks for the JSON. Always calls back exactly once. */
    void fetch(final Result cb, final int timeoutMs) {
        final Handler main = new Handler(Looper.getMainLooper());
        main.post(new Runnable() { public void run() {
            try {
                web = new WebView(ctx);
                WebSettings s = web.getSettings();
                s.setJavaScriptEnabled(true);
                s.setDomStorageEnabled(true);
                s.setDatabaseEnabled(true);
                // never announce ourselves as a WebView: Cloudflare scores "; wv)" user agents lower
                s.setUserAgentString(s.getUserAgentString().replace("; wv", ""));
                web.setWebViewClient(new WebViewClient() {
                    @Override public void onPageFinished(WebView v, String url) {
                        // give Cloudflare's script a moment to settle before asking
                        main.postDelayed(new Runnable() { public void run() { ask(cb); } }, 6000);
                    }
                    @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                        if (req != null && req.isForMainFrame()) finish(cb, false, "page failed: " + err.getDescription());
                    }
                });
                web.loadUrl(PAGE);
                main.postDelayed(new Runnable() { public void run() {
                    finish(cb, false, "timed out after " + (timeoutMs / 1000) + "s");
                } }, timeoutMs);
            } catch (Throwable t) {
                finish(cb, false, t.getClass().getSimpleName() + ": " + t.getMessage());
            }
        } });
    }

    private void ask(final Result cb) {
        if (done || web == null) return;
        web.evaluateJavascript(JS, new ValueCallback<String>() {
            public void onReceiveValue(String value) {
                // value is a JSON string literal containing our JSON — unwrap it
                String inner = Json.unquote(value);
                int status = Json.intField(inner, "status");
                String body = Json.stringField(inner, "body");
                if (status == 200 && body != null && body.startsWith("{")) finish(cb, true, body);
                else finish(cb, false, status == 0 ? "no response from VFS" : ("VFS answered " + status));
            }
        });
    }

    private void finish(final Result cb, final boolean ok, final String payload) {
        if (done) return;
        done = true;
        if (web != null) { web.stopLoading(); web.destroy(); web = null; }
        if (ok) cb.ok(payload); else cb.failed(payload);
    }
}
