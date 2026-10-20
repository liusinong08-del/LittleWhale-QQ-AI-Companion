package cn.littlewhale.app;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.ActivityNotFoundException;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.webkit.*;
import android.widget.*;
import java.io.ByteArrayInputStream;

/** A dedicated window for the existing private core; no model key is copied into this APK. */
public final class WebApp extends Activity {
    WebView web;
    TextView status;
    boolean ready;
    long reconnectAfter;
    ValueCallback<Uri[]> imageChooser;
    final Handler clock=new Handler(Looper.getMainLooper());
    boolean paired(){return Build.VERSION.SDK_INT==Target.SDK&&Target.ID.equals(Settings.Secure.getString(getContentResolver(),Settings.Secure.ANDROID_ID));}
    static boolean local(String value){
        if(value==null)return false;
        Uri u=Uri.parse(value);
        return "http".equals(u.getScheme())&&"127.0.0.1".equals(u.getHost())&&u.getPort()==8767&&u.getUserInfo()==null;
    }
    static boolean panel(String value){return value!=null&&value.matches("http://127\\.0\\.0\\.1:8767/#[A-Za-z0-9_-]{43}");}
    boolean navigate(String url){
        if(local(url))return false;
        if(paired()&&local(web.getUrl())&&url!=null&&url.matches("intent://pair/#Intent;scheme=littlewhale;package=cn\\.littlewhale\\.bridge;S\\.token=[A-Za-z0-9_-]{43};end")){
            String token=url.substring(url.indexOf("S.token=")+8,url.length()-4);
            try{startActivity(new Intent().setClassName("cn.littlewhale.bridge","cn.littlewhale.bridge.Main").putExtra("token",token));}
            catch(Exception error){Toast.makeText(this,"请先安装虚拟机消息助手",Toast.LENGTH_SHORT).show();}
        }
        return true;
    }
    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);
        LinearLayout box=new LinearLayout(this);box.setOrientation(1);
        LinearLayout bar=new LinearLayout(this);
        status=new TextView(this);status.setText("正在连接小鲸鱼……");status.setPadding(16,12,8,12);
        bar.addView(status,new LinearLayout.LayoutParams(0,-2,1));
        Button settings=new Button(this);settings.setText("设置");settings.setOnClickListener(v->web.evaluateJavascript("document.querySelector('[data-tab=settings]').click()",null));bar.addView(settings);
        Button retry=new Button(this);retry.setText("重新连接");retry.setOnClickListener(v->connect());bar.addView(retry);box.addView(bar);
        web=new WebView(this);box.addView(web,new LinearLayout.LayoutParams(-1,0,1));setContentView(box);
        if(!paired()){status.setText("请在配对的小鲸鱼虚拟机中打开");retry.setEnabled(false);return;}
        WebSettings s=web.getSettings();s.setJavaScriptEnabled(true);s.setDomStorageEnabled(true);
        web.addJavascriptInterface(new Object(){
            @JavascriptInterface public void reconnect(){clock.post(()->{
                if(!paired()||!local(web.getUrl())||SystemClock.elapsedRealtime()<reconnectAfter)return;
                reconnectAfter=SystemClock.elapsedRealtime()+15000;connect();
            });}
        },"WhaleApp");
        s.setAllowFileAccess(false);s.setAllowContentAccess(true);s.setAllowFileAccessFromFileURLs(false);s.setAllowUniversalAccessFromFileURLs(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebChromeClient(new WebChromeClient(){
            @Override public boolean onShowFileChooser(WebView view,ValueCallback<Uri[]> callback,FileChooserParams params){
                if(!paired()||!local(view.getUrl()))return false;
                if(imageChooser!=null)imageChooser.onReceiveValue(null);
                imageChooser=callback;
                Intent pick=new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("image/*").addCategory(Intent.CATEGORY_OPENABLE);
                pick.putExtra(Intent.EXTRA_MIME_TYPES,new String[]{"image/jpeg","image/png","image/gif","image/webp"});
                try{startActivityForResult(pick,2);}catch(ActivityNotFoundException error){imageChooser=null;callback.onReceiveValue(null);Toast.makeText(WebApp.this,"虚拟机中没有可用的图片选择器",Toast.LENGTH_LONG).show();}
                return true;
            }
        });
        web.setWebViewClient(new WebViewClient(){
            @Override public boolean shouldOverrideUrlLoading(WebView view,String url){return navigate(url);}
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){return navigate(request.getUrl().toString());}
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request){
                if(local(request.getUrl().toString()))return null;
                return new WebResourceResponse("text/plain","UTF-8",403,"Local core only",null,new ByteArrayInputStream(new byte[0]));
            }
            @Override public void onPageFinished(WebView view,String url){if(local(url)){ready=true;status.setText("小鲸鱼 · 本机空间");web.evaluateJavascript("document.querySelector('[data-tab=social]').click()",null);}}
            @Override public void onReceivedError(WebView view,WebResourceRequest request,WebResourceError error){
                if(request.isForMainFrame()){ready=false;status.setText("尚未连上核心，请重新连接");}
            }
        });
        if(!accept(getIntent()))connect();
    }
    boolean accept(Intent intent){
        String url=intent.getStringExtra("panel_url");
        if(!paired()||!panel(url))return false;
        // The per-run link stays in memory. The API key remains in Termux's private core settings.
        intent.removeExtra("panel_url");web.loadUrl(url);return true;
    }
    @Override public void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);if(!accept(intent))connect();}
    void connect(){
        if(!paired())return;
        if(checkSelfPermission("com.termux.permission.RUN_COMMAND")!=PackageManager.PERMISSION_GRANTED){
            status.setText("允许连接虚拟机内的小鲸鱼核心后即可打开");requestPermissions(new String[]{"com.termux.permission.RUN_COMMAND"},1);return;
        }
        ready=false;status.setText("正在连接小鲸鱼……");
        // Fixed command only: read the private panel link and deliver it to this explicit activity, never a browser or log.
        String command="set -eu; umask 077; printf 'started\\n' > /sdcard/LittleWhale-source/app-connect.log; [ \"$(/system/bin/getprop ro.build.version.sdk)\" = "+Target.SDK+" ]; url=; IFS= read -r url < \"$HOME/.littlewhale/panel-url\" || [ -n \"$url\" ]; printf 'link_read\\n' >> /sdcard/LittleWhale-source/app-connect.log; case \"$url\" in http://127.0.0.1:8767/\\#*) ;; *) exit 1 ;; esac; errors=\"$HOME/.littlewhale/app-launch-errors.log\"; if LD_LIBRARY_PATH=/system/lib64:/vendor/lib64 /system/bin/timeout -k 2 15 /system/bin/am start --user 0 -n cn.littlewhale.app/.WebApp --es panel_url \"$url\" >\"$errors\" 2>&1; then printf 'activity_submitted\\n' >> /sdcard/LittleWhale-source/app-connect.log; else printf 'activity_failed\\n' >> /sdcard/LittleWhale-source/app-connect.log; token=${url#*#}; while IFS= read -r line; do line=${line//\"$url\"/[private-link]}; line=${line//\"$token\"/[private-token]}; printf '%s\\n' \"$line\" >> /sdcard/LittleWhale-source/app-connect.log; done <\"$errors\"; fi";
        // Background RUN_COMMAND omits /system/bin; Android's am script needs app_process on PATH.
        command="PATH=/system/bin:/system/xbin:$PATH; export PATH; "+command;
        Intent run=new Intent("com.termux.RUN_COMMAND").setClassName("com.termux","com.termux.app.RunCommandService");
        run.putExtra("com.termux.RUN_COMMAND_PATH","/data/data/com.termux/files/usr/bin/bash");
        run.putExtra("com.termux.RUN_COMMAND_ARGUMENTS",new String[]{"-c",command});
        run.putExtra("com.termux.RUN_COMMAND_WORKDIR","/data/data/com.termux/files/home");
        run.putExtra("com.termux.RUN_COMMAND_BACKGROUND",true);
        try{if(startService(run)==null)throw new IllegalStateException();}
        catch(Exception error){status.setText("请先在虚拟机启动小鲸鱼核心");return;}
        clock.postDelayed(()->{if(!ready)status.setText("尚未连接，请确认核心已启动后重新连接");},18000);
    }
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] results){super.onRequestPermissionsResult(request,permissions,results);if(request==1&&results.length>0&&results[0]==PackageManager.PERMISSION_GRANTED)connect();}
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(request!=2||imageChooser==null)return;
        ValueCallback<Uri[]> callback=imageChooser;imageChooser=null;
        Uri uri=result==RESULT_OK&&data!=null?data.getData():null;
        callback.onReceiveValue(paired()&&uri!=null&&"content".equals(uri.getScheme())?new Uri[]{uri}:null);
    }
    @Override public void onBackPressed(){if(web.canGoBack())web.goBack();else super.onBackPressed();}
    @Override public void onDestroy(){clock.removeCallbacksAndMessages(null);if(imageChooser!=null){imageChooser.onReceiveValue(null);imageChooser=null;}if(web!=null)web.destroy();super.onDestroy();}
}
