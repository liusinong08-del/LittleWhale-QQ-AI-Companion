package cn.littlewhale.bridge;

import android.app.*;
import android.content.*;
import android.os.*;
import android.widget.TextView;
import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.*;

/** Emulator-only local fixture. Never included in the production VM APK. */
public final class Fixture extends Activity {
    @Override public void onCreate(Bundle saved){super.onCreate(saved);
        if(getIntent().getBooleanExtra("boot_check",false)){new Main.Boot().onReceive(this,new Intent(Intent.ACTION_BOOT_COMPLETED));finish();return;}
        NotificationManager manager=(NotificationManager)getSystemService(NOTIFICATION_SERVICE);
        if(Build.VERSION.SDK_INT>=26)manager.createNotificationChannel(new NotificationChannel("fixture","本地检查",NotificationManager.IMPORTANCE_DEFAULT));
        Notification.Builder builder=Build.VERSION.SDK_INT>=26?new Notification.Builder(this,"fixture"):new Notification.Builder(this);
        Intent args=getIntent();long at=args.getLongExtra("time",System.currentTimeMillis());
        String title=args.getStringExtra("title"),text=args.getStringExtra("text");
        Notification.MessagingStyle style=new Notification.MessagingStyle("小鲸鱼");style.addMessage(text,at,"测试主人");
        builder.setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle(title).setContentText(text).setWhen(at).setStyle(style);
        if(args.getBooleanExtra("reply",true)){
            PendingIntent target=PendingIntent.getBroadcast(this,1,new Intent(this,Receiver.class),PendingIntent.FLAG_UPDATE_CURRENT|(Build.VERSION.SDK_INT>=31?PendingIntent.FLAG_MUTABLE:0));
            RemoteInput input=new RemoteInput.Builder("reply").setLabel("回复").build();
            builder.addAction(new Notification.Action.Builder(android.R.drawable.ic_menu_send,"回复",target).addRemoteInput(input).build());
        }
        manager.notify(100,builder.build());TextView view=new TextView(this);view.setText("本地通知检查，不连接任何通讯账号。");setContentView(view);
    }
    public static final class Receiver extends BroadcastReceiver {
        @Override public void onReceive(Context context,Intent intent){try{
            Bundle result=RemoteInput.getResultsFromIntent(intent);if(result==null)return;
            File file=new File(context.getFilesDir(),"replies.json");JSONArray replies=new JSONArray();
            if(file.exists()){ByteArrayOutputStream out=new ByteArrayOutputStream();try(InputStream in=new FileInputStream(file)){byte[] b=new byte[1024];int n;while((n=in.read(b))!=-1)out.write(b,0,n);}replies=new JSONArray(new String(out.toByteArray(),StandardCharsets.UTF_8));}
            replies.put(result.getCharSequence("reply").toString());try(OutputStream out=new FileOutputStream(file)){out.write(replies.toString().getBytes(StandardCharsets.UTF_8));}
        }catch(Exception e){throw new IllegalStateException("Local reply fixture failed",e);}}
    }
}
