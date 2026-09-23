package com.fourindegree.vfswatch;

import android.util.Log;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;

/**
 * A small SOCKS5 server, so the PC can borrow this phone's mobile connection.
 *
 * The PC bridges a local port to it with `adb forward tcp:1080 tcp:1080` and points the bot's
 * browser at socks5://127.0.0.1:1080. Traffic then leaves through the phone's carrier IP — a
 * residential-grade address that Cloudflare treats far better than any datacentre one — while the
 * PC's own traffic stays on the office line. Toggling the phone's mobile data gives a new IP.
 *
 * Only what SOCKS5 needs for this job: no authentication (it is reachable over USB only), CONNECT
 * to an IPv4/IPv6/hostname target, then raw byte shuffling in both directions.
 */
class SocksServer implements Runnable {

    static final int PORT = 1080;
    private static final String TAG = "vfswatch.socks";

    private final ExecutorService pool = Executors.newCachedThreadPool();
    private ServerSocket server;
    private volatile boolean running;
    static final AtomicLong connections = new AtomicLong();
    static final AtomicLong bytes = new AtomicLong();

    void start() {
        if (running) return;
        running = true;
        new Thread(this, "socks-accept").start();
    }

    void stop() {
        running = false;
        try { if (server != null) server.close(); } catch (IOException ignored) { }
        pool.shutdownNow();
    }

    boolean isRunning() { return running; }

    @Override public void run() {
        try {
            // bind to loopback: only adb forward (USB) can reach it, never the phone's network
            server = new ServerSocket(PORT, 64, InetAddress.getByName("127.0.0.1"));
            Log.i(TAG, "listening on 127.0.0.1:" + PORT);
            while (running) {
                final Socket client = server.accept();
                connections.incrementAndGet();
                pool.execute(new Runnable() { public void run() { serve(client); } });
            }
        } catch (IOException e) {
            if (running) Log.w(TAG, "accept stopped: " + e);
        } finally {
            running = false;
        }
    }

    private void serve(Socket client) {
        Socket target = null;
        try {
            client.setSoTimeout(60_000);
            InputStream in = client.getInputStream();
            OutputStream out = client.getOutputStream();

            // --- greeting: version, how many auth methods, the methods themselves
            if (read(in) != 0x05) return;
            int methods = read(in);
            for (int i = 0; i < methods; i++) read(in);
            out.write(new byte[]{0x05, 0x00});          // "no authentication needed"
            out.flush();

            // --- request: version, command, reserved, address type
            if (read(in) != 0x05) return;
            int cmd = read(in);
            read(in);
            int atyp = read(in);
            String host;
            if (atyp == 0x01) {                          // IPv4
                byte[] a = readN(in, 4);
                host = (a[0] & 0xff) + "." + (a[1] & 0xff) + "." + (a[2] & 0xff) + "." + (a[3] & 0xff);
            } else if (atyp == 0x03) {                   // hostname
                host = new String(readN(in, read(in)), "US-ASCII");
            } else if (atyp == 0x04) {                   // IPv6
                host = InetAddress.getByAddress(readN(in, 16)).getHostAddress();
            } else {
                reply(out, 0x08); return;                // address type not supported
            }
            int port = (read(in) << 8) | read(in);

            if (cmd != 0x01) { reply(out, 0x07); return; }   // only CONNECT

            try {
                target = new Socket();
                target.connect(new InetSocketAddress(host, port), 20_000);
                target.setSoTimeout(60_000);
            } catch (IOException e) {
                reply(out, 0x05);                        // connection refused
                return;
            }
            reply(out, 0x00);                            // succeeded

            // --- relay both ways until either side closes
            final Socket t = target;
            Thread up = new Thread(new Runnable() { public void run() { pipe(clientIn(client), outOf(t)); } });
            up.start();
            pipe(inOf(t), out);
            up.interrupt();
        } catch (Throwable ignored) {
        } finally {
            close(client);
            close(target);
        }
    }

    // ---- plumbing ---------------------------------------------------------------

    private void pipe(InputStream in, OutputStream out) {
        byte[] buf = new byte[16 * 1024];
        try {
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                out.flush();
                bytes.addAndGet(n);
            }
        } catch (IOException ignored) { }
    }

    private void reply(OutputStream out, int status) throws IOException {
        out.write(new byte[]{0x05, (byte) status, 0x00, 0x01, 0, 0, 0, 0, 0, 0});
        out.flush();
    }

    private int read(InputStream in) throws IOException {
        int b = in.read();
        if (b < 0) throw new IOException("closed");
        return b;
    }

    private byte[] readN(InputStream in, int n) throws IOException {
        byte[] b = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(b, off, n - off);
            if (r < 0) throw new IOException("closed");
            off += r;
        }
        return b;
    }

    private InputStream clientIn(Socket s) { try { return s.getInputStream(); } catch (IOException e) { return null; } }
    private InputStream inOf(Socket s)     { try { return s.getInputStream(); } catch (IOException e) { return null; } }
    private OutputStream outOf(Socket s)   { try { return s.getOutputStream(); } catch (IOException e) { return null; } }
    private void close(Socket s) { try { if (s != null) s.close(); } catch (IOException ignored) { } }
}
