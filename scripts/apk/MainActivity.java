package com.selfmail.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.net.http.SslError;
import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.SslErrorHandler;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

/** SelfMail 安卓客户端：WebView 全屏加载自托管 Webmail。 */
public class MainActivity extends Activity {
    private WebView web;
    private SharedPreferences prefs;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("selfmail", MODE_PRIVATE);
        web = new WebView(this);
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);
        web.setWebViewClient(new WebViewClient() {
            // 自托管环境常见自签名证书：放行并继续
            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.proceed();
            }
        });
        setContentView(web);
        String url = prefs.getString("server", "");
        if (url.length() == 0) {
            askServer();
        } else {
            web.loadUrl(url);
        }
    }

    private void askServer() {
        final EditText input = new EditText(this);
        input.setHint("https://mail.example.com");
        new AlertDialog.Builder(this)
                .setTitle("连接你的 SelfMail 服务器")
                .setMessage("输入服务器地址（含 https:// 与端口），例如 https://mail.example.com")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("连接", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface dialog, int which) {
                        String u = input.getText().toString().trim();
                        if (u.length() > 0) {
                            if (!u.startsWith("http")) u = "https://" + u;
                            prefs.edit().putString("server", u).commit();
                            web.loadUrl(u);
                        }
                    }
                })
                .show();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
