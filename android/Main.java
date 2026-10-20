package cn.littlewhale.bridge;

import android.app.*;
import android.content.*;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.*;
import android.provider.Settings;
import android.service.notification.*;
import android.text.InputType;
import android.widget.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import org.json.*;

public final class Main extends Activity {
    static final String PREFS="bridge", VERSION="0.1";
    static volatile boolean connected;
    static final java.util.concurrent.atomic.AtomicBoolean checkingNetwork=new java.util.concurrent.atomic.AtomicBoolean();
    static boolean pairedDevice(Context c) {
        return Build.VERSION.SDK_INT==Target.SDK && Target.ID.equals(Settings.Secure.getString(c.getContentResolver(),Settings.Secure.ANDROID_ID));
    }
    static String token(Context c) { return c.getSharedPreferences(PREFS,0).getString("token",""); }
    public static final class Control extends ContentProvider {
        ClipboardManager clipboard;
        @Override public boolean onCreate(){
            // Android 7 constructs an internal Handler: initialize on the main thread, before Binder calls.
            clipboard=(ClipboardManager)getContext().getSystemService(CLIPBOARD_SERVICE);return true;
        }
        void authorize(){int uid=Binder.getCallingUid();if(!pairedDevice(getContext())||(uid!=2000&&uid!=android.os.Process.myUid()&&getContext().getPackageManager().checkSignatures(uid,android.os.Process.myUid())!=android.content.pm.PackageManager.SIGNATURE_MATCH))throw new SecurityException("Paired VM controller required");}
        @Override public Bundle call(String method,String arg,Bundle extras){authorize();
            if("update_core".equals(method)&&arg==null&&Binder.getCallingUid()==2000&&Target.FIXTURE.isEmpty()){
                long caller=Binder.clearCallingIdentity();try{updateCore(getContext());}finally{Binder.restoreCallingIdentity(caller);}
                Bundle result=new Bundle();result.putBoolean("update_requested",true);return result;
            }
            if(!"clipboard".equals(method)||arg==null||arg.length()>12000)throw new IllegalArgumentException("Unsupported input operation");
            String value;
            try{value=StandardCharsets.UTF_8.newDecoder().onMalformedInput(java.nio.charset.CodingErrorAction.REPORT).decode(java.nio.ByteBuffer.wrap(android.util.Base64.decode(arg,android.util.Base64.NO_WRAP))).toString();}
            catch(Exception e){throw new IllegalArgumentException("Invalid UTF-8 input");}
            if(value.isEmpty()||value.length()>4000||value.indexOf('\0')>=0)throw new IllegalArgumentException("Invalid input length");
            // This is the guest clipboard only. Clipboard sharing with the physical phone stays disabled.
            clipboard.setPrimaryClip(ClipData.newPlainText("LittleWhale",value));
            Bundle result=new Bundle();result.putBoolean("clipboard_set",true);return result;
        }
        @Override public android.database.Cursor query(android.net.Uri u,String[] p,String s,String[] a,String o){authorize();throw new UnsupportedOperationException();}
        @Override public String getType(android.net.Uri u){authorize();return "application/json";}
        @Override public android.net.Uri insert(android.net.Uri u,ContentValues v){authorize();throw new UnsupportedOperationException();}
        @Override public int delete(android.net.Uri u,String s,String[] a){authorize();throw new UnsupportedOperationException();}
        @Override public int update(android.net.Uri u,ContentValues v,String s,String[] a){authorize();throw new UnsupportedOperationException();}
    }
    static void runPython(Context c,String payload,String action){
        String prefix="";
        if(action!=null){
            if(!action.matches("qemustop|qemustart|qemurestart"))throw new IllegalArgumentException("Invalid managed action");
            prefix="export LITTLEWHALE_P0_ACTION='"+action+"'; ";
        }
        String script=prefix+"set -eu; export PATH=/system/bin:/system/xbin:$PATH; cd \"$HOME/LittleWhale\"; runtime_path=; IFS= read -r runtime_path < .runtime-path || [ -n \"$runtime_path\" ]; case \"$runtime_path\" in \"$HOME\"/.littlewhale-runtime-*) ;; *) exit 1 ;; esac; export PYTHONHOME=\"$runtime_path\" LD_LIBRARY_PATH=\"$runtime_path/lib\" SSL_CERT_FILE=\"$runtime_path/etc/tls/cert.pem\"; exec \"$runtime_path/bin/python3.14\" -c 'import base64; exec(base64.b64decode(\""+payload+"\"))'";
        Intent command=new Intent("com.termux.RUN_COMMAND").setClassName("com.termux","com.termux.app.RunCommandService");
        command.putExtra("com.termux.RUN_COMMAND_PATH","/data/data/com.termux/files/usr/bin/bash");
        command.putExtra("com.termux.RUN_COMMAND_ARGUMENTS",new String[]{"-c",script});
        command.putExtra("com.termux.RUN_COMMAND_WORKDIR","/data/data/com.termux/files/home/LittleWhale");
        command.putExtra("com.termux.RUN_COMMAND_BACKGROUND",true);
        if(c.startService(command)==null)throw new IllegalStateException("Termux not available");
    }
    static void runPython(Context c,String payload){runPython(c,payload,null);}
    static void updateCore(Context c){
        if(!pairedDevice(c)||Target.UPDATE.isEmpty())throw new SecurityException("Paired production helper required");
        runPython(c,Target.UPDATE);
    }
    static void runCheck(Context c,String action){
        if(!pairedDevice(c)||Target.P0CHECK.isEmpty())throw new SecurityException("Paired production helper required");
        runPython(c,Target.P0CHECK,action);
    }
    static void runCheck(Context c){runCheck(c,null);}
    public static final class Update extends Activity {
        @Override public void onCreate(Bundle saved){
            super.onCreate(saved);
            // A DUMP-protected, no-window maintenance entry preserves the user's foreground app.
            try{updateCore(this);}catch(Exception error){android.util.Log.w("LittleWhale","Update not submitted: "+error.getClass().getSimpleName());}
            finally{finish();}
        }
    }
    public static final class Check extends Activity {
        @Override public void onCreate(Bundle saved){
            super.onCreate(saved);
            // A DUMP-protected, no-window maintenance entry preserves the user's foreground app.
            try{runCheck(this);}catch(Exception error){android.util.Log.w("LittleWhale","Check not submitted: "+error.getClass().getSimpleName());}
            finally{finish();}
        }
    }
    static void termux(Context c, boolean open) {
        if(!pairedDevice(c))throw new SecurityException("Wrong paired device");
        if(open){c.startActivity(new Intent().setClassName("cn.littlewhale.app","cn.littlewhale.app.WebApp").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));return;}
        Intent command=new Intent("com.termux.RUN_COMMAND");
        command.setClassName("com.termux","com.termux.app.RunCommandService");
        command.putExtra("com.termux.RUN_COMMAND_PATH","/data/data/com.termux/files/usr/bin/bash");
        command.putExtra("com.termux.RUN_COMMAND_ARGUMENTS",new String[]{"/data/data/com.termux/files/home/LittleWhale/guest-start.sh","--no-browser"});
        command.putExtra("com.termux.RUN_COMMAND_WORKDIR","/data/data/com.termux/files/home/LittleWhale");
        command.putExtra("com.termux.RUN_COMMAND_BACKGROUND",true);
        c.startService(command);
    }
    public static final class Boot extends BroadcastReceiver {
        @Override public void onReceive(Context c,Intent intent){
            if(!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())||!pairedDevice(c)||!c.getSharedPreferences(PREFS,0).getBoolean("boot",false))return;
            try{termux(c,false);}catch(Exception error){android.util.Log.w("LittleWhale","Boot recovery not submitted: "+error.getClass().getSimpleName());}
        }
    }
    static JSONObject request(Context c,String path,JSONObject body) throws Exception {
        if (!pairedDevice(c) || token(c).isEmpty()) throw new IOException("Not paired");
        HttpURLConnection connection=(HttpURLConnection)new URL("http://127.0.0.1:8767/bridge/"+path).openConnection();
        connection.setConnectTimeout(4000); connection.setReadTimeout(75000); connection.setInstanceFollowRedirects(false);
        connection.setRequestMethod("POST"); connection.setRequestProperty("Authorization","Bearer "+token(c));
        connection.setRequestProperty("Content-Type","application/json"); connection.setDoOutput(true);
        byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8); connection.setFixedLengthStreamingMode(bytes.length);
        try {
            try(OutputStream out=connection.getOutputStream()) { out.write(bytes); }
            int code=connection.getResponseCode();
            // 202 marks a durably queued social event; 200 is every other bridge call.
            if(code!=200&&code!=202) throw new IOException("HTTP "+code);
            ByteArrayOutputStream out=new ByteArrayOutputStream();
            try(InputStream in=connection.getInputStream()) { byte[] buffer=new byte[4096]; int count;
                while((count=in.read(buffer))!=-1) { out.write(buffer,0,count); if(out.size()>24000)throw new IOException("Response too large"); }
            }
            return new JSONObject(new String(out.toByteArray(),StandardCharsets.UTF_8));
        } finally { connection.disconnect(); }
    }
    @Override public void onCreate(Bundle saved) { super.onCreate(saved); show(getIntent()); }
    @Override public void onNewIntent(Intent intent) { super.onNewIntent(intent); setIntent(intent); show(intent); }
    void checkNetwork(TextView result) {
        if(!pairedDevice(this)||!checkingNetwork.compareAndSet(false,true))return;
        result.setText("正在检查实际HTTPS连接……");
        new Thread(()->{
            JSONArray items=new JSONArray();StringBuilder summary=new StringBuilder();
            try{
                for(String domain:new String[]{"api-docs.deepseek.com","im.qq.com","weixin.qq.com"}){
                    JSONObject item=new JSONObject().put("domain",domain).put("https_reachable",false);
                    long start=SystemClock.elapsedRealtime();HttpURLConnection connection=null;
                    try{
                        item.put("dns_answers",InetAddress.getAllByName(domain).length);
                        connection=(HttpURLConnection)new URL("https://"+domain+"/").openConnection();
                        connection.setConnectTimeout(6000);connection.setReadTimeout(6000);connection.setInstanceFollowRedirects(false);
                        int status=connection.getResponseCode();item.put("status",status).put("https_reachable",true);
                        summary.append(domain).append("：HTTPS已连接，HTTP ").append(status).append("\n");
                    }catch(Exception error){item.put("error",error.getClass().getSimpleName());summary.append(domain).append("：").append(error.getClass().getSimpleName()).append("\n");}
                    finally{if(connection!=null)connection.disconnect();}
                    item.put("seconds",(SystemClock.elapsedRealtime()-start)/1000.0);items.put(item);
                    String progress=summary.toString();runOnUiThread(()->result.setText(progress));
                }
                File folder=getExternalFilesDir(null);if(folder==null)throw new IOException("Storage unavailable");folder.mkdirs();
                File temporary=new File(folder,"network-check.tmp"),output=new File(folder,"network-check.json");
                JSONObject report=new JSONObject().put("checked_at_ms",System.currentTimeMillis()).put("results",items);
                try(FileOutputStream out=new FileOutputStream(temporary)){out.write(report.toString(2).getBytes(StandardCharsets.UTF_8));out.getFD().sync();}
                if(!temporary.renameTo(output))throw new IOException("Report save failed");
                runOnUiThread(()->Toast.makeText(this,"网络检查完成",0).show());
            }catch(Exception error){runOnUiThread(()->Toast.makeText(this,"网络检查记录未能保存",0).show());}
            finally{checkingNetwork.set(false);}
        }).start();
    }
    void show(Intent intent) {
        LinearLayout box=new LinearLayout(this); box.setOrientation(1); box.setPadding(32,32,32,32);
        ScrollView scroll=new ScrollView(this);scroll.addView(box);
        TextView title=new TextView(this); title.setTextSize(25); title.setText("小鲸鱼消息助手"); box.addView(title);
        TextView info=new TextView(this); info.setText("仅在配对的独立虚拟手机中工作。读取QQ、微信通知中的文字，并使用通知自带的快捷回复。\n\n在小鲸鱼页面授权具体会话后才会自动回复。没有快捷回复的通知不能发送。\n"); box.addView(info);
        if(!pairedDevice(this)) { info.append("\n当前设备不是配对的虚拟机，所有接入已停止。\n配对诊断："+Build.VERSION.SDK_INT+" / "+Settings.Secure.getString(getContentResolver(),Settings.Secure.ANDROID_ID)); setContentView(scroll); return; }
        EditText key=new EditText(this); key.setHint("从小鲸鱼生活设置配对"); key.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);
        String incoming=intent.getStringExtra("token"); key.setText(incoming==null?token(this):incoming); box.addView(key);
        Button save=new Button(this); save.setText("保存配对"); box.addView(save);
        save.setOnClickListener(v->{String value=key.getText().toString().trim(); if(!value.matches("[A-Za-z0-9_-]{43}")){Toast.makeText(this,"配对信息格式不正确",0).show();return;}
            if(!getSharedPreferences(PREFS,0).edit().putString("token",value).commit()){Toast.makeText(this,"配对保存失败，请检查存储空间",0).show();return;}
            if(!Target.FIXTURE.isEmpty())android.util.Log.w("LittleWhale","pair-save");
            new Thread(()->{
                try{request(this,"status",new JSONObject().put("connected",connected).put("sdk",Build.VERSION.SDK_INT).put("version",VERSION)); runOnUiThread(()->Toast.makeText(this,"配对成功",0).show());}
                catch(Exception e){runOnUiThread(()->Toast.makeText(this,"尚未连上核心，请确认服务和配对信息",0).show());}
            }).start();});
        Button permission=new Button(this); permission.setText("开启虚拟机通知访问权限"); box.addView(permission);
        permission.setOnClickListener(v->startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));
        Button corePermission=new Button(this);corePermission.setText("允许助手恢复小鲸鱼服务");box.addView(corePermission);
        corePermission.setOnClickListener(v->requestPermissions(new String[]{"com.termux.permission.RUN_COMMAND"},1));
        Switch boot=new Switch(this);boot.setText("虚拟机重启后启动小鲸鱼");boot.setChecked(getSharedPreferences(PREFS,0).getBoolean("boot",false));box.addView(boot);
        boot.setOnCheckedChangeListener((button,on)->{if(!getSharedPreferences(PREFS,0).edit().putBoolean("boot",on).commit())Toast.makeText(this,"设置保存失败",0).show();});
        for(boolean open:new boolean[]{false,true}){Button button=new Button(this);button.setText(open?"打开小鲸鱼空间":"启动或恢复小鲸鱼");box.addView(button);
            button.setOnClickListener(v->{try{termux(this,open);Toast.makeText(this,"已请求Termux"+(open?"打开页面":"恢复服务")+"，请稍候",0).show();}catch(Exception error){Toast.makeText(this,"请先授权助手，并在Termux完成恢复配置",0).show();}});}
        TextView state=new TextView(this); state.setText("\n通知服务："+(connected?"已连接":"未连接")+"\n模型密钥保留在Python核心，助手不持有模型密钥。\n只读已存在的通知不会触发自动回复。"); box.addView(state);
        Button network=new Button(this);network.setText("检查虚拟机网络");box.addView(network);
        TextView networkResult=new TextView(this);box.addView(networkResult);network.setOnClickListener(v->checkNetwork(networkResult));
        setContentView(scroll);
        if(intent.getBooleanExtra("network_check",false))checkNetwork(networkResult);
    }

    public static final class Listener extends NotificationListenerService {
        // ponytail: a phone has one model worker; bound notification bursts to 64, then await a fresh update.
        final ThreadPoolExecutor worker=new ThreadPoolExecutor(1,1,0,TimeUnit.SECONDS,new ArrayBlockingQueue<Runnable>(64),new ThreadPoolExecutor.DiscardPolicy());
        final ScheduledExecutorService clock=Executors.newSingleThreadScheduledExecutor();
        final Map<String,Message> current=new HashMap<>();
        final Map<String,ArrayDeque<String>> sent=new HashMap<>();
        volatile boolean live;
        static final class Message { String thread,event,pkg,title,text,key; Notification.Action action; }
        private static String mediaText(CharSequence value){
            String s=string(value);return s.substring(0,s.offsetByCodePoints(0,Math.min(200,s.codePointCount(0,s.length()))));
        }
        private JSONObject music() throws JSONException {
            JSONObject result=new JSONObject().put("available",false).put("track",JSONObject.NULL);
            if(!live)return result;
            try{
                MediaSessionManager manager=(MediaSessionManager)getSystemService(MEDIA_SESSION_SERVICE);
                JSONObject selected=null;
                for(MediaController controller:manager.getActiveSessions(new ComponentName(this,Listener.class))){
                    MediaMetadata metadata=controller.getMetadata();PlaybackState playback=controller.getPlaybackState();
                    if(metadata==null||playback==null)continue;
                    CharSequence title=metadata.getText(MediaMetadata.METADATA_KEY_TITLE);
                    if(title==null)title=metadata.getDescription().getTitle();
                    String name=mediaText(title);if(name.isEmpty())continue;
                    int state=playback.getState();
                    String label=state==PlaybackState.STATE_PLAYING?"playing":state==PlaybackState.STATE_PAUSED?"paused":state==PlaybackState.STATE_STOPPED?"stopped":state==PlaybackState.STATE_BUFFERING||state==PlaybackState.STATE_CONNECTING?"buffering":"unknown";
                    JSONObject track=new JSONObject().put("package",controller.getPackageName()).put("title",name)
                        .put("artist",mediaText(metadata.getText(MediaMetadata.METADATA_KEY_ARTIST))).put("state",label)
                        .put("position_ms",Math.max(-1,Math.min(604800000,playback.getPosition())))
                        .put("duration_ms",Math.max(0,Math.min(604800000,metadata.getLong(MediaMetadata.METADATA_KEY_DURATION))));
                    // ponytail: one current player keeps context small; expose a list if concurrent playback is needed.
                    if(selected==null)selected=track;
                    if(state==PlaybackState.STATE_PLAYING){selected=track;break;}
                }
                return result.put("available",true).put("track",selected==null?JSONObject.NULL:selected);
            }catch(RuntimeException unavailable){return result;}
        }
        @Override public void onCreate() { super.onCreate(); clock.scheduleWithFixedDelay(()->worker.execute(()->report()),5,30,TimeUnit.SECONDS); }
        void report(){
            try{
                if(!pairedDevice(this))return;
                JSONObject result=request(this,"status",new JSONObject().put("connected",live).put("sdk",Build.VERSION.SDK_INT).put("version",VERSION).put("music",music()));

            }catch(Exception error){
                String message=error.getMessage();android.util.Log.w("LittleWhale","status: "+error.getClass().getSimpleName()+((message!=null&&(message.matches("HTTP [0-9]{3}")||message.equals("Not paired")))?" "+message:""));
            }
        }
        @Override public void onListenerConnected(){live=true;connected=true; if(!pairedDevice(this))return;
            try{for(StatusBarNotification n:getActiveNotifications())record(n,false);}catch(Exception ignored){} reportAsync();}
        void reportAsync(){worker.execute(()->report());}
        @Override public void onListenerDisconnected(){live=false;connected=false; synchronized(current){current.clear();} reportAsync();}
        @Override public void onDestroy(){live=false;connected=false;clock.shutdownNow();worker.shutdownNow();super.onDestroy();}
        @Override public void onNotificationPosted(StatusBarNotification n){record(n,true);}
        @Override public void onNotificationRemoved(StatusBarNotification n){synchronized(current){Iterator<Message> i=current.values().iterator();while(i.hasNext())if(i.next().key.equals(n.getKey()))i.remove();}}
        static String hash(String value)throws Exception{byte[] bytes=MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));StringBuilder out=new StringBuilder();for(byte b:bytes)out.append(String.format(Locale.ROOT,"%02x",b&255));return out.toString();}
        static String string(CharSequence value){return value==null?"":value.toString();}
        static Message inspect(StatusBarNotification sbn)throws Exception {
            Notification n=sbn.getNotification(); if((n.flags&Notification.FLAG_GROUP_SUMMARY)!=0)return null;
            Bundle b=n.extras; String title=string(b.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE));
            if(title.isEmpty())title=string(b.getCharSequence(Notification.EXTRA_TITLE));
            String text=string(b.getCharSequence(Notification.EXTRA_TEXT)); long at=n.when>0?n.when:sbn.getPostTime();
            Parcelable[] messages=b.getParcelableArray(Notification.EXTRA_MESSAGES);
            if(messages!=null&&messages.length>0&&messages[messages.length-1] instanceof Bundle){
                Bundle latest=(Bundle)messages[messages.length-1]; String sender=string(latest.getCharSequence("sender"));
                if(Build.VERSION.SDK_INT>=28&&sender.isEmpty()){Person person=latest.getParcelable("sender_person");if(person!=null)sender=string(person.getName());}
                if(sender.isEmpty())return null; // MessagingStyle uses a null sender for the local user's messages.
                text=sender+"："+string(latest.getCharSequence("text")); at=latest.getLong("time",at);
            }
            if(title.trim().isEmpty()||title.length()>200||text.trim().isEmpty()||text.length()>4000)return null;
            if(title.matches(".*[0-9]+个联系人.*")||text.matches("^\\[?[0-9]+条.*(消息|未读).*"))return null;
            Message m=new Message(); m.pkg=sbn.getPackageName();m.title=title;m.text=text;m.key=sbn.getKey();
            String identity=Build.VERSION.SDK_INT>=26?n.getShortcutId():null;
            if(identity==null||identity.isEmpty())identity=Objects.toString(sbn.getTag(),"")+":"+sbn.getId()+":"+title;
            m.thread=hash(m.pkg+"\n"+identity);m.event=hash(m.thread+"\n"+text+"\n"+at);
            if(n.actions!=null)for(Notification.Action action:n.actions){
                if(action.actionIntent==null||action.actionIntent.isActivity()||!m.pkg.equals(action.actionIntent.getCreatorPackage())||action.getRemoteInputs()==null)continue;
                for(RemoteInput input:action.getRemoteInputs())if(input.getAllowFreeFormInput()){m.action=action;break;}
                if(m.action!=null)break;
            }
            return m;
        }
        void record(StatusBarNotification n,boolean fresh){
            if(!live||!pairedDevice(this)||!(n.getPackageName().equals("com.tencent.mobileqq")||n.getPackageName().equals("com.tencent.mm")||(!Target.FIXTURE.isEmpty()&&n.getPackageName().equals(Target.FIXTURE))))return;
            try{Message m=inspect(n);if(m==null)return; synchronized(current){
                ArrayDeque<String> own=sent.get(m.thread);if(own!=null)for(String value:own)if(m.text.equals(value)||m.text.endsWith("："+value))return;
                if(!current.containsKey(m.thread)&&current.size()>=100)return;current.put(m.thread,m);
            } worker.execute(()->receive(m,fresh));}catch(Exception ignored){}
        }
        boolean valid(Message m){Message latest=current.get(m.thread);return live&&pairedDevice(this)&&latest!=null&&latest.event.equals(m.event)&&latest.action!=null;}
        void receive(Message m,boolean fresh){
            try{
                JSONObject result=request(this,"event",new JSONObject().put("package",m.pkg).put("thread",m.thread).put("event",m.event).put("title",m.title).put("message",m.text).put("replyable",m.action!=null).put("fresh",fresh));
                // The core stores the event and generates in its own worker; wait for that, never resend.
                long until=SystemClock.elapsedRealtime()+90000;
                while(("queued".equals(result.optString("status"))||"working".equals(result.optString("status")))&&SystemClock.elapsedRealtime()<until){
                    synchronized(current){if(!valid(m))return;}
                    Thread.sleep(700);
                    result=request(this,"pending",new JSONObject().put("event",m.event).put("thread",m.thread));
                }
                if(!"generated".equals(result.optString("status")))return;
                JSONArray parts=result.optJSONArray("parts");
                if(parts==null||parts.length()==0)return;
                for(int i=0;i<parts.length();i++){
                    synchronized(current){if(!valid(m))return;}
                    int part=parts.getInt(i);
                    // Delay before claiming, so a new notification or user pause can cancel the pending reply.
                    Thread.sleep(i==0?900:1500);
                    synchronized(current){if(!valid(m))return;}
                    JSONObject claim=request(this,"claim",new JSONObject().put("event",m.event).put("thread",m.thread).put("part",part));
                    if(!claim.optBoolean("send"))return;
                    String status="cancelled",content=claim.getString("content");
                    synchronized(current){if(valid(m)){
                        ArrayDeque<String> own=sent.get(m.thread);if(own==null){own=new ArrayDeque<>();sent.put(m.thread,own);}own.add(content);while(own.size()>12)own.removeFirst();
                        try{Intent reply=new Intent();Bundle answers=new Bundle();for(RemoteInput input:m.action.getRemoteInputs())if(input.getAllowFreeFormInput())answers.putCharSequence(input.getResultKey(),content);
                            RemoteInput.addResultsToIntent(m.action.getRemoteInputs(),reply,answers);m.action.actionIntent.send(this,0,reply);status="submitted";
                        }catch(Exception e){status="send_failed";}
                    }}
                    request(this,"ack",new JSONObject().put("event",m.event).put("part",part).put("status",status));
                    if(!status.equals("submitted"))return;
                }
            }catch(Exception ignored){/* No automatic resend after a timeout or uncertain PendingIntent submission. */}
        }
    }
}
