package com.fourindegree.vfswatch;

import android.content.Context;
import android.content.SharedPreferences;

/** Settings plus the last snapshot, so the screen always has something to show. */
class Prefs {
    private static final String FILE = "vfswatch";
    static final String CATEGORY = "category", PHONE = "phone", PHONE2 = "phone2", SMS = "sms",
            INTERVAL_MIN = "interval",
            LAST_JSON = "last_json", LAST_AT = "last_at", LAST_ERROR = "last_error",
            NEXT_AT = "next_at", ALERTED = "alerted";

    static SharedPreferences of(Context c) { return c.getSharedPreferences(FILE, Context.MODE_PRIVATE); }

    static String category(Context c) { return of(c).getString(CATEGORY, "D visa"); }
    static String phone(Context c)    { return of(c).getString(PHONE, ""); }
    static String phone2(Context c)   { return of(c).getString(PHONE2, ""); }
    static boolean sms(Context c)     { return of(c).getBoolean(SMS, true); }
    static void put(Context c, String key, boolean v) { of(c).edit().putBoolean(key, v).apply(); }
    /** How long to wait when we cannot work out VFS's own cycle. */
    static int intervalMin(Context c) { return of(c).getInt(INTERVAL_MIN, 60); }

    static void put(Context c, String key, String v) { of(c).edit().putString(key, v).apply(); }
    static void put(Context c, String key, long v)   { of(c).edit().putLong(key, v).apply(); }
    static void put(Context c, String key, int v)    { of(c).edit().putInt(key, v).apply(); }
}
