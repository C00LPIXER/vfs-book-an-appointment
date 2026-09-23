package com.fourindegree.vfswatch;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** The whole app: what VFS says, when it was said, and when we will look again. */
public class MainActivity extends Activity {

    private TextView updated, updatedRaw, checked, next, error, json, openTitle, openBody, tabList, tabJson, live;
    private LinearLayout list, jsonBox, openBanner;
    private EditText phone, phone2;
    private android.widget.CheckBox smsOn;
    private boolean showJson;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final SimpleDateFormat clock = new SimpleDateFormat("h:mm a", Locale.ENGLISH);

    private final BroadcastReceiver onChecked = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) { render(); }
    };
    private final Runnable ticker = new Runnable() {
        public void run() { render(); ui.postDelayed(this, 30_000); }   // keep "x min ago" honest
    };

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.main);
        updated = findViewById(R.id.updated);   updatedRaw = findViewById(R.id.updatedRaw);
        checked = findViewById(R.id.checked);   next = findViewById(R.id.next);
        error = findViewById(R.id.error);       json = findViewById(R.id.json);
        list = findViewById(R.id.list);         jsonBox = findViewById(R.id.jsonBox);
        openBanner = findViewById(R.id.openBanner);
        openTitle = findViewById(R.id.openTitle); openBody = findViewById(R.id.openBody);
        tabList = findViewById(R.id.tabList);   tabJson = findViewById(R.id.tabJson);
        live = findViewById(R.id.live);         phone = findViewById(R.id.phone);
        phone2 = findViewById(R.id.phone2);     smsOn = findViewById(R.id.smsOn);

        Alerter.ensureChannels(this);
        Alerter.stopRinging();
        askPermissions();
        phone.setText(Prefs.phone(this));
        phone2.setText(Prefs.phone2(this));
        smsOn.setChecked(Prefs.sms(this));

        findViewById(R.id.save).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Prefs.put(MainActivity.this, Prefs.PHONE, phone.getText().toString().trim());
                Prefs.put(MainActivity.this, Prefs.PHONE2, phone2.getText().toString().trim());
                Prefs.put(MainActivity.this, Prefs.SMS, smsOn.isChecked());
                toast("Saved");
            }
        });
        findViewById(R.id.refresh).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { check(); }
        });
        findViewById(R.id.test).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                Alerter.slotFound(MainActivity.this, "TEST — D VISA OPEN", "Chennai — 28 Sep 2026 (Long Stay D visa)");
                Alerter.startRinging(MainActivity.this);
                ui.postDelayed(new Runnable() { public void run() { Alerter.stopRinging(); } }, 6000);
                if (Prefs.sms(MainActivity.this)) {
                    int n = Alerter.sms(MainActivity.this, "TEST — VFS D-visa watch: this is how a slot alert looks.",
                            Prefs.phone(MainActivity.this), Prefs.phone2(MainActivity.this));
                    toast(n > 0 ? ("Test SMS sent to " + n + " number(s)") : "SMS not sent — check the Phone/SMS permission");
                }
                Alerter.call(MainActivity.this, Prefs.phone(MainActivity.this));
            }
        });
        findViewById(R.id.copy).setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                ((ClipboardManager) getSystemService(CLIPBOARD_SERVICE))
                        .setPrimaryClip(ClipData.newPlainText("vfs", Prefs.of(MainActivity.this).getString(Prefs.LAST_JSON, "")));
                toast("JSON copied");
            }
        });
        tabList.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showJson = false; tabs(); }
        });
        tabJson.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showJson = true; tabs(); }
        });

        AlarmReceiver.schedule(this, Prefs.intervalMin(this));
        tabs();
        render();
        if (Prefs.of(this).getLong(Prefs.LAST_AT, 0) == 0) check();
    }

    @Override protected void onResume() {
        super.onResume();
        registerReceiver(onChecked, new IntentFilter(PollService.ACTION_DONE),
                Build.VERSION.SDK_INT >= 33 ? Context.RECEIVER_NOT_EXPORTED : 0);
        Alerter.stopRinging();
        ui.post(ticker);
    }

    @Override protected void onPause() {
        super.onPause();
        ui.removeCallbacks(ticker);
        try { unregisterReceiver(onChecked); } catch (Throwable ignored) { }
    }

    private void tabs() {
        tabList.setBackgroundResource(showJson ? R.drawable.tab_off : R.drawable.tab_on);
        tabList.setTextColor(showJson ? 0xFF8B97A8 : 0xFF0B0E13);
        tabJson.setBackgroundResource(showJson ? R.drawable.tab_on : R.drawable.tab_off);
        tabJson.setTextColor(showJson ? 0xFF0B0E13 : 0xFF8B97A8);
        list.setVisibility(showJson ? View.GONE : View.VISIBLE);
        jsonBox.setVisibility(showJson ? View.VISIBLE : View.GONE);
    }

    private void check() {
        updated.setText("checking…");
        live.setTextColor(0xFFFF9A3C);
        Intent svc = new Intent(this, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc); else startService(svc);
    }

    // ---- drawing ----------------------------------------------------------------

    private void render() {
        String body = Prefs.of(this).getString(Prefs.LAST_JSON, "");
        long at = Prefs.of(this).getLong(Prefs.LAST_AT, 0);
        long nextAt = Prefs.of(this).getLong(Prefs.NEXT_AT, 0);
        String err = Prefs.of(this).getString(Prefs.LAST_ERROR, "");

        error.setVisibility(err.isEmpty() ? View.GONE : View.VISIBLE);
        error.setText(err);
        live.setTextColor(err.isEmpty() ? 0xFF2E7D4F : 0xFFE5534B);
        checked.setText(at > 0 ? clock.format(new Date(at)) + "  (" + ago(at) + ")" : "—");
        next.setText(nextAt > 0 ? clock.format(new Date(nextAt)) + "  (in " + until(nextAt) + ")" : "—");

        if (body.isEmpty()) {
            updated.setText(err.isEmpty() ? "no data yet" : "cannot read VFS");
            updatedRaw.setText("");
            list.removeAllViews();
            return;
        }

        Json.Snapshot s = Json.parse(body, Prefs.category(this));
        long mins = s.updatedValue < 0 ? -1 : s.updatedValue * unit(s.updatedType)
                + (at > 0 ? (System.currentTimeMillis() - at) / 60000 : 0);
        updated.setText(mins < 0 ? s.updatedOn : (mins < 60 ? mins + " min ago"
                : (mins / 60) + " h " + (mins % 60) + " min ago"));
        updatedRaw.setText(s.updatedOn + "   ·   " + s.centres + " centres");

        json.setText(pretty(body));

        if (s.open.isEmpty()) {
            openBanner.setVisibility(View.GONE);
        } else {
            openBanner.setVisibility(View.VISIBLE);
            openTitle.setText(s.open.size() == 1 ? "D VISA OPEN" : "D VISA OPEN — " + s.open.size() + " centres");
            StringBuilder sb = new StringBuilder();
            for (Json.Row r : s.open) sb.append(r.centre).append(" — ").append(r.date).append('\n');
            openBody.setText(sb.toString().trim());
        }
        drawList(body);
    }

    /** One row per centre: name on the left, its date (or "no date") as a pill on the right. */
    private void drawList(String body) {
        list.removeAllViews();
        String want = Prefs.category(this).toLowerCase();
        List<String[]> rows = new ArrayList<>();
        try {
            JSONArray vac = new JSONObject(body).optJSONArray("vacList");
            for (int i = 0; vac != null && i < vac.length(); i++) {
                JSONObject v = vac.getJSONObject(i);
                String centre = Json.shortCentre(v.optString("vacName", ""));
                String date = "";
                boolean has = false;
                JSONArray g = v.optJSONArray("visaGroupList");
                for (int j = 0; g != null && j < g.length(); j++) {
                    JSONObject x = g.getJSONObject(j);
                    if (x.optString("displayName", "").toLowerCase().contains(want)) {
                        has = true;
                        date = x.optString("earliestAvailableDate", "");
                    }
                }
                rows.add(new String[]{centre, has ? (date.isEmpty() ? "no date" : date) : "not offered"});
            }
        } catch (Exception ignored) { }

        for (String[] r : rows) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(dp(10), dp(10), dp(10), dp(10));

            TextView name = new TextView(this);
            name.setText(r[0]);
            name.setTextColor(0xFFE6EAF2);
            name.setTextSize(14);
            name.setLayoutParams(new LinearLayout.LayoutParams(0, -2, 1f));

            boolean open = !r[1].equals("no date") && !r[1].equals("not offered");
            TextView pill = new TextView(this);
            pill.setText(r[1]);
            pill.setTextColor(open ? 0xFF5BD98A : 0xFF6B7686);
            pill.setTextSize(12);
            pill.setTypeface(Typeface.MONOSPACE, open ? Typeface.BOLD : Typeface.NORMAL);
            pill.setBackgroundResource(open ? R.drawable.pill_ok : R.drawable.pill_none);
            pill.setPadding(dp(12), dp(6), dp(12), dp(6));

            row.addView(name);
            row.addView(pill);
            list.addView(row);
        }
        if (rows.isEmpty()) {
            TextView t = new TextView(this);
            t.setText("No centres in the response.");
            t.setTextColor(0xFF6B7686);
            t.setPadding(dp(10), dp(10), dp(10), dp(10));
            list.addView(t);
        }
    }

    // ---- small helpers ----------------------------------------------------------

    private String pretty(String body) {
        try { return new JSONObject(body).toString(2); } catch (Exception e) { return body; }
    }

    private String ago(long at) {
        long m = (System.currentTimeMillis() - at) / 60000;
        return m < 1 ? "just now" : m < 60 ? m + " min ago" : (m / 60) + " h ago";
    }

    private String until(long when) {
        long m = (when - System.currentTimeMillis()) / 60000;
        return m <= 0 ? "due" : m < 60 ? m + " min" : (m / 60) + " h " + (m % 60) + " min";
    }

    private long unit(String type) {
        if ("H".equalsIgnoreCase(type)) return 60;
        if ("D".equalsIgnoreCase(type)) return 1440;
        if ("S".equalsIgnoreCase(type)) return 0;
        return 1;
    }

    private int dp(int v) { return (int) (v * getResources().getDisplayMetrics().density); }

    private void toast(String s) { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); }

    private void askPermissions() {
        if (Build.VERSION.SDK_INT >= 23) {
            List<String> need = new ArrayList<>();
            if (Build.VERSION.SDK_INT >= 33
                    && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.POST_NOTIFICATIONS);
            if (checkSelfPermission(Manifest.permission.CALL_PHONE) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.CALL_PHONE);
            if (checkSelfPermission(Manifest.permission.SEND_SMS) != PackageManager.PERMISSION_GRANTED)
                need.add(Manifest.permission.SEND_SMS);
            if (!need.isEmpty()) requestPermissions(need.toArray(new String[0]), 7);
        }
    }
}
