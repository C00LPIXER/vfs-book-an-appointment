package com.fourindegree.vfswatch;

import android.content.Context;
import android.content.SharedPreferences;

/** Everything the user can set, plus the last snapshot so the screen has something to show. */
class Prefs {
    private static final String FILE = "vfswatch";
    static final String CATEGORY = "category", PHONE = "phone", INTERVAL_MIN = "interval",
            LAST_JSON = "last_json", LAST_AT = "last_at", LAST_ERROR = "last_error",
            ALERTED = "alerted", RINGING = "ringing";

    static SharedPreferences of(Context c) { return c.getSharedPreferences(FILE, Context.MODE_PRIVATE); }

    static String category(Context c) { return of(c).getString(CATEGORY, "D visa"); }
    static String phone(Context c)    { return of(c).getString(PHONE, ""); }
    static int intervalMin(Context c) { return of(c).getInt(INTERVAL_MIN, 60); }

    static void put(Context c, String key, String v) { of(c).edit().putString(key, v).apply(); }
    static void put(Context c, String key, long v)   { of(c).edit().putLong(key, v).apply(); }
    static void put(Context c, String key, int v)    { of(c).edit().putInt(key, v).apply(); }
}
