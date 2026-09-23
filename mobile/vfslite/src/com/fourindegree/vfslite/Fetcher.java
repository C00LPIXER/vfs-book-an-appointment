package com.fourindegree.vfslite;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.util.Map;

/**
 * Reads VFS's public "earliest available date" endpoint — the data behind the site's
 * "Find the Earliest Available Appointment Date" box.
 *
 * Three things matter here, and each of them broke the first attempts:
 *
 *  1. Cloudflare scores the TLS fingerprint. HttpURLConnection/OkHttp get 403205; Chromium gets 200,
 *     so the call is made inside a WebView sitting on the real VFS page.
 *  2. The request must look like the site's own — same origin and cookies, the x-auth-token the SPA
 *     sends, and no invented headers (an extra one turns it into a CORS preflight VFS refuses).
 *  3. `evaluateJavascript` does NOT wait for a Promise: an async function hands back "null" straight
 *     away. The result therefore comes back through a JavascriptInterface callback instead.
 */
class Fetcher {

    interface Result {
        void ok(String json);
        void failed(String reason);
    }

    private static final String PAGE = "https://visa.vfsglobal.com/ind/en/bgr/";
    private static final String ENDPOINT = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot";

    private final Context ctx;
    private final Handler main = new Handler(Looper.getMainLooper());
    private WebView web;
    private Result cb;
    private boolean done;
    private int tries;
    private String token;               // x-auth-token, copied from the page's own request
    private String lastError = "";

    Fetcher(Context ctx) { this.ctx = ctx; }

    void fetch(final Result cb, final int timeoutMs) {
        this.cb = cb;
        main.post(new Runnable() { public void run() {
            try {
                web = new WebView(ctx);
                WebSettings s = web.getSettings();
                s.setJavaScriptEnabled(true);
                s.setDomStorageEnabled(true);
                s.setDatabaseEnabled(true);
                s.setUserAgentString(s.getUserAgentString().replace("; wv", ""));
                web.addJavascriptInterface(new Bridge(), "VfsBridge");

                web.setWebViewClient(new WebViewClient() {
                    @Override public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest req) {
                        try {   // the page calls the endpoint itself — borrow its auth header
                            if (req != null && req.getUrl() != null
                                    && req.getUrl().toString().toLowerCase().contains("centerwithearliestslot")) {
                                Map<String, String> h = req.getRequestHeaders();
                                if (h != null) for (Map.Entry<String, String> e : h.entrySet())
                                    if (e.getKey().equalsIgnoreCase("x-auth-token")) token = e.getValue();
                            }
                        } catch (Throwable ignored) { }
                        return null;
                    }
                    @Override public void onPageFinished(WebView v, String url) {
                        main.postDelayed(new Runnable() { public void run() { ask(); } }, 5000);
                    }
                    @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                        if (req != null && req.isForMainFrame()) lastError = "page: " + err.getDescription();
                    }
                });

                web.loadUrl(PAGE);
                main.postDelayed(new Runnable() { public void run() {
                    finish(false, lastError.isEmpty() ? ("no answer within " + (timeoutMs / 1000) + "s")
                                                      : (lastError + " (gave up after " + (timeoutMs / 1000) + "s)"));
                } }, timeoutMs);
            } catch (Throwable t) {
                finish(false, t.getClass().getSimpleName() + ": " + t.getMessage());
            }
        } });
    }

    /** Runs the site's own request and hands the answer back through the bridge. */
    private void ask() {
        if (done || web == null) return;
        tries++;
        String hdr = "'content-type':'application/json'";
        if (token != null && !token.isEmpty()) hdr += ",'x-auth-token':'" + token.replace("'", "") + "'";
        String js =
            "(function(){ try {" +
            "  fetch('" + ENDPOINT + "', {method:'POST', credentials:'include'," +
            "     headers:{" + hdr + "}," +
            "     body: JSON.stringify({missionCode:'bgr',countryCode:'ind',cultureCode:'en-US'})})" +
            "   .then(function(r){ return r.text().then(function(t){ VfsBridge.result(r.status, t); }); })" +
            "   .catch(function(e){ VfsBridge.result(0, String(e)); });" +
            "} catch (e) { VfsBridge.result(0, 'threw ' + String(e)); } })()";
        web.evaluateJavascript(js, null);
    }

    /** JS calls this when the request finishes — it runs on a WebView thread, so hop to main. */
    private class Bridge {
        @JavascriptInterface public void result(final int status, final String body) {
            main.post(new Runnable() { public void run() {
                if (done) return;
                if (status == 200 && body != null && body.trim().startsWith("{")) { finish(true, body); return; }
                lastError = (status == 0 ? "network: " : ("VFS answered " + status + " "))
                        + (body == null ? "" : body.replaceAll("\\s+", " ").trim());
                if (tries >= 8) { finish(false, lastError); return; }
                main.postDelayed(new Runnable() { public void run() { ask(); } }, 6000);
            } });
        }
    }

    private void finish(final boolean ok, final String payload) {
        if (done) return;
        done = true;
        if (web != null) { web.stopLoading(); web.destroy(); web = null; }
        if (ok) cb.ok(payload); else cb.failed(payload);
    }
}
