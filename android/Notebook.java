package cn.littlewhale.notes;

import android.app.*;
import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import android.graphics.Color;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.text.*;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.*;

/** Two offline applications share code, but Android gives each a separate private database. */
public final class Notebook extends Activity {
    static boolean paired(Context c) {
        return Build.VERSION.SDK_INT==Target.SDK && Target.ID.equals(Settings.Secure.getString(c.getContentResolver(),Settings.Secure.ANDROID_ID));
    }
    static String clean(JSONObject o,String key,int max,boolean empty) throws JSONException {
        Object value=o.get(key);
        if(!(value instanceof String))throw new IllegalArgumentException("文字格式不正确");
        String s=((String)value).trim();
        if((!empty&&s.isEmpty())||s.length()>max||s.indexOf('\0')>=0)throw new IllegalArgumentException("文字为空或超过长度限制");
        return s;
    }
    static final class Store extends SQLiteOpenHelper {
        Store(Context c){super(c,"entries.sqlite3",null,1);setWriteAheadLoggingEnabled(true);}
        @Override public void onCreate(SQLiteDatabase db){db.execSQL("CREATE TABLE entries(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,created INTEGER NOT NULL,updated INTEGER NOT NULL,source TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0)");}
        @Override public void onUpgrade(SQLiteDatabase db,int a,int b){throw new IllegalStateException("Unsupported database upgrade; keep existing data");}
        JSONObject row(Cursor c)throws JSONException{
            JSONObject o=new JSONObject();for(String key:new String[]{"id","title","body","source"})o.put(key,c.getString(c.getColumnIndexOrThrow(key)));
            for(String key:new String[]{"created","updated","deleted"})o.put(key,c.getLong(c.getColumnIndexOrThrow(key)));return o;
        }
        JSONObject save(JSONObject input,boolean ai)throws JSONException{
            String id=clean(input,"id",120,false),title=clean(input,"title",120,false),body=clean(input,"body",12000,false);
            if(!id.matches("[A-Za-z0-9:_-]{1,120}"))throw new IllegalArgumentException("记录标识无效");
            long now=System.currentTimeMillis();SQLiteDatabase db=getWritableDatabase();db.beginTransaction();
            try(Cursor c=db.query("entries",null,"id=?",new String[]{id},null,null,null)){
                JSONObject previous=c.moveToFirst()?row(c):null;
                // ponytail: preserve manual edits and deletions; resolve conflicts in the app before resuming AI updates.
                if(ai&&previous!=null&&(!previous.getString("source").equals("ai")||previous.getLong("deleted")!=0))return previous;
                ContentValues values=new ContentValues();values.put("id",id);values.put("title",title);values.put("body",body);
                values.put("created",previous==null?now:previous.getLong("created"));values.put("updated",now);values.put("source",ai?"ai":"manual");values.put("deleted",0);
                db.insertWithOnConflict("entries",null,values,SQLiteDatabase.CONFLICT_REPLACE);db.setTransactionSuccessful();return input;
            }finally{db.endTransaction();}
        }
        JSONArray list(String search,boolean includeDeleted)throws JSONException{
            JSONArray result=new JSONArray();String clause=includeDeleted?"1=1":"deleted=0";String[] args=null;
            if(search!=null&&!search.isEmpty()){clause+=" AND (instr(title,?)>0 OR instr(body,?)>0)";args=new String[]{search,search};}
            try(Cursor c=getReadableDatabase().query("entries",null,clause,args,null,null,"created DESC,id DESC")){
                while(c.moveToNext())result.put(row(c));
            }return result;
        }
        JSONArray page(String after)throws JSONException{
            JSONArray rows=new JSONArray();try(Cursor c=getReadableDatabase().query("entries",null,"id>?",new String[]{after},null,null,"id ASC","5")){while(c.moveToNext())rows.put(row(c));}return rows;
        }
        int[] merge(JSONArray entries){
            int added=0,changed=0,skipped=0,bad=0;SQLiteDatabase db=getWritableDatabase();db.beginTransaction();
            try{
                for(int i=0;i<entries.length();i++){
                    JSONObject input=entries.optJSONObject(i);String id,title,body,source;long created,updated;int deleted;
                    try{
                        if(input==null)throw new IllegalArgumentException();
                        id=clean(input,"id",120,false);
                        if(!id.matches("[A-Za-z0-9:_-]{1,120}"))throw new IllegalArgumentException("记录标识无效");
                        title=clean(input,"title",120,false);body=clean(input,"body",12000,false);
                        source=input.getString("source");
                        if(!source.equals("ai")&&!source.equals("manual"))throw new IllegalArgumentException("记录来源无效");
                        created=input.getLong("created");updated=input.getLong("updated");deleted=input.optInt("deleted",0);
                        if(created<0||updated<0||(deleted!=0&&deleted!=1))throw new IllegalArgumentException("记录时间无效");
                    }catch(Exception e){bad++;continue;}
                    try(Cursor c=db.query("entries",null,"id=?",new String[]{id},null,null,null)){
                        JSONObject previous=c.moveToFirst()?row(c):null;
                        // ponytail: manual records always win; older AI backups never overwrite newer local AI text.
                        boolean apply=previous==null||(!previous.getString("source").equals("manual")&&(source.equals("manual")||updated>previous.getLong("updated")));
                        if(!apply){skipped++;continue;}
                        ContentValues values=new ContentValues();values.put("id",id);values.put("title",title);values.put("body",body);
                        values.put("created",previous==null?created:previous.getLong("created"));
                        values.put("updated",updated);values.put("source",source);values.put("deleted",deleted);
                        db.insertWithOnConflict("entries",null,values,SQLiteDatabase.CONFLICT_REPLACE);
                        if(previous==null)added++;else changed++;
                    }catch(JSONException e){bad++;}
                }
                db.setTransactionSuccessful();
            }finally{db.endTransaction();}
            return new int[]{added,changed,skipped,bad};
        }
        void remove(String id){ContentValues v=new ContentValues();v.put("deleted",1);v.put("source","manual");v.put("updated",System.currentTimeMillis());getWritableDatabase().update("entries",v,"id=?",new String[]{id});}
    }
    Store store;LinearLayout cards;EditText search;String query="";
    final android.database.ContentObserver changes=new android.database.ContentObserver(new Handler()){public void onChange(boolean self){if(store!=null)refresh();}};
    String title(){return Target.KIND.equals("diary")?"小鲸鱼日记":"小鲸鱼记忆";}
    int dp(int n){return Math.round(n*getResources().getDisplayMetrics().density);}
    void message(String text){Toast.makeText(this,text,Toast.LENGTH_LONG).show();}
    TextView label(String text,int size){TextView v=new TextView(this);v.setText(text);v.setTextSize(size);v.setTextColor(Color.rgb(24,66,70));return v;}
    Button button(String text,LinearLayout box,View.OnClickListener click){Button b=new Button(this);b.setText(text);b.setOnClickListener(click);box.addView(b);return b;}
    @Override public void onCreate(Bundle saved){super.onCreate(saved);
        if(!paired(this)){setContentView(label("此应用只在配对的小鲸鱼虚拟机内运行。",20));return;}
        store=new Store(this);LinearLayout root=new LinearLayout(this);root.setOrientation(1);root.setPadding(dp(16),dp(12),dp(16),dp(8));root.setBackgroundColor(Color.rgb(245,248,240));
        root.addView(label(title(),28));root.addView(label(Target.KIND.equals("diary")?"把今天想留下的事，慢慢写下来。":"记住重要的人、偏好和共同经历。",16));
        root.addView(label("离线保存于本应用；卸载会删除私有数据，请先导出备份。",12));
        LinearLayout controls=new LinearLayout(this);root.addView(controls);button("写一条",controls,v->edit(null));button("导出备份",controls,v->export());button("导入备份",controls,v->restore());
        search=new EditText(this);search.setSingleLine(true);search.setHint("搜索标题或内容");search.setContentDescription("搜索记录");root.addView(search);
        search.addTextChangedListener(new TextWatcher(){public void beforeTextChanged(CharSequence s,int a,int c,int f){}public void onTextChanged(CharSequence s,int a,int b,int c){query=s.toString();refresh();}public void afterTextChanged(Editable e){}});
        ScrollView scroll=new ScrollView(this);cards=new LinearLayout(this);cards.setOrientation(1);scroll.addView(cards);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));setContentView(root);refresh();
    }
    @Override public void onResume(){super.onResume();if(store!=null){getContentResolver().registerContentObserver(Uri.parse("content://"+getPackageName()+".entries"),true,changes);refresh();}}
    @Override public void onPause(){getContentResolver().unregisterContentObserver(changes);super.onPause();}
    String date(long at){SimpleDateFormat f=new SimpleDateFormat("yyyy年MM月dd日 HH:mm",Locale.CHINA);f.setTimeZone(TimeZone.getTimeZone("Asia/Shanghai"));return f.format(new Date(at));}
    void refresh(){if(cards==null)return;cards.removeAllViews();try{
        JSONArray rows=store.list(query,false);
        if(rows.length()==0)cards.addView(label(query.isEmpty()?"还没有记录，写下第一条吧。":"没有找到相关记录。",16));
        // ponytail: render at most 200 matching cards; search still scans all records, add paging when needed.
        for(int i=0;i<Math.min(200,rows.length());i++){
            JSONObject row=rows.getJSONObject(i);LinearLayout card=new LinearLayout(this);card.setOrientation(1);card.setPadding(dp(10),dp(12),dp(10),dp(12));card.setBackgroundColor(Color.WHITE);
            LinearLayout.LayoutParams spacing=new LinearLayout.LayoutParams(-1,-2);spacing.bottomMargin=dp(10);cards.addView(card,spacing);
            card.addView(label(row.getString("title"),20));card.addView(label(date(row.getLong("created"))+(row.getString("source").equals("ai")?" · 小鲸鱼写下":" · 手动记录"),12));
            TextView body=label(row.getString("body"),16);body.setTextIsSelectable(true);card.addView(body);
            LinearLayout actions=new LinearLayout(this);card.addView(actions);button("修改",actions,v->edit(row));button("删除",actions,v->new AlertDialog.Builder(this).setTitle("删除这条记录？").setMessage("导出的备份会保留。AI不会自动重新导入这条记录。").setNegativeButton("取消",null).setPositiveButton("删除",(d,w)->{store.remove(row.optString("id"));refresh();}).show());
        }
        if(rows.length()>200)cards.addView(label("当前显示前200条，请搜索更早的内容。",14));
    }catch(Exception e){message("读取失败，已有记录已保留。请检查可用空间。");}}
    void edit(JSONObject previous){
        LinearLayout box=new LinearLayout(this);box.setOrientation(1);box.setPadding(dp(20),dp(8),dp(20),dp(8));
        EditText heading=new EditText(this);heading.setSingleLine(true);heading.setHint(Target.KIND.equals("diary")?"日记标题":"记忆主题");heading.setFilters(new InputFilter[]{new InputFilter.LengthFilter(120)});box.addView(heading);
        EditText body=new EditText(this);body.setHint("写下想保存的内容");body.setGravity(Gravity.TOP);body.setMinLines(5);body.setMaxLines(10);body.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE|android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);body.setFilters(new InputFilter[]{new InputFilter.LengthFilter(12000)});box.addView(body);
        if(previous!=null){heading.setText(previous.optString("title"));body.setText(previous.optString("body"));}else if(Target.KIND.equals("diary"))heading.setText(date(System.currentTimeMillis()).substring(0,11)+"的日记");
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle(previous==null?"写一条":"修改记录").setView(box).setNegativeButton("取消",null).setPositiveButton("保存",null).create();
        dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{try{
            JSONObject row=new JSONObject().put("id",previous==null?UUID.randomUUID().toString():previous.getString("id")).put("title",heading.getText().toString()).put("body",body.getText().toString());
            store.save(row,false);dialog.dismiss();refresh();message("已保存在本机");
        }catch(IllegalArgumentException e){body.setError(e.getMessage());}catch(Exception e){message("保存失败，文字仍在，请检查存储空间。");}}));dialog.show();
    }
    void export(){Intent i=new Intent(Intent.ACTION_CREATE_DOCUMENT);i.setType("application/json");i.addCategory(Intent.CATEGORY_OPENABLE);i.putExtra(Intent.EXTRA_TITLE,Target.KIND+"-"+new SimpleDateFormat("yyyyMMdd-HHmmss",Locale.US).format(new Date())+".json");startActivityForResult(i,1);}
    void restore(){Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT);i.setType("application/json");i.addCategory(Intent.CATEGORY_OPENABLE);startActivityForResult(i,2);}
    void importBackup(Uri uri){
        try(InputStream stream=getContentResolver().openInputStream(uri)){
            if(stream==null)throw new IOException();
            ByteArrayOutputStream buffer=new ByteArrayOutputStream();byte[] chunk=new byte[8192];int n;
            while((n=stream.read(chunk))>0){buffer.write(chunk,0,n);if(buffer.size()>8_000_000)throw new IllegalArgumentException("备份文件超过8MB");}
            JSONObject backup=new JSONObject(new String(buffer.toByteArray(),StandardCharsets.UTF_8));
            if(!backup.optString("format").equals("LittleWhaleNotes-v1")||!backup.optString("kind").equals(Target.KIND))throw new IllegalArgumentException("这不是本应用的备份文件");
            JSONArray entries=backup.optJSONArray("entries");if(entries==null)throw new IllegalArgumentException("备份内容不完整");
            new AlertDialog.Builder(this).setTitle("导入备份？").setMessage("将合并 "+entries.length()+" 条记录；手动记录和较新的本地记录会保留，重复导入不会重复。")
                .setNegativeButton("取消",null).setPositiveButton("导入",(d,w)->{int[] counts=store.merge(entries);refresh();message("导入完成：新增"+counts[0]+"，更新"+counts[1]+"，保留本地"+counts[2]+"，跳过损坏"+counts[3]+"条");}).show();
        }catch(Exception e){message(e instanceof IllegalArgumentException&&e.getMessage()!=null?e.getMessage():"导入失败；本机记录仍保留。");}
    }
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(result!=RESULT_OK||data==null||data.getData()==null)return;if(request==2){importBackup(data.getData());return;}if(request!=1)return;try{
        byte[] bytes=new JSONObject().put("format","LittleWhaleNotes-v1").put("kind",Target.KIND).put("entries",store.list(null,true)).toString(2).getBytes(StandardCharsets.UTF_8);
        try(OutputStream stream=getContentResolver().openOutputStream(data.getData(),"wt")){if(stream==null)throw new IOException();stream.write(bytes);stream.flush();}message("备份已导出");
    }catch(Exception e){message("导出失败；本机记录仍保留，请重新选择保存位置。");}}
    @Override protected void onDestroy(){if(store!=null)store.close();super.onDestroy();}

    public static final class Provider extends ContentProvider {
        Store store;
        @Override public boolean onCreate(){store=new Store(getContext());return true;}
        void authorize(){
            if(!paired(getContext()))throw new SecurityException("Wrong paired VM");
            int caller=Binder.getCallingUid();
            // The paired VM's ADB shell is the AI control channel; ordinary installed apps need our signing key.
            if(caller!=android.os.Process.myUid()&&caller!=2000&&getContext().getPackageManager().checkSignatures(caller,android.os.Process.myUid())!=android.content.pm.PackageManager.SIGNATURE_MATCH)throw new SecurityException("Private notebook");
        }
        @Override public Bundle call(String method,String arg,Bundle extras){authorize();try{
            Bundle result=new Bundle();JSONObject data;
            if("put".equals(method)){if(arg==null||arg.length()>50000)throw new IllegalArgumentException("Input too large");data=store.save(new JSONObject(arg),true);getContext().getContentResolver().notifyChange(Uri.parse("content://"+getContext().getPackageName()+".entries"),null);}
            else if("export".equals(method)){
                String after=arg==null?"":arg;if(!after.matches("[A-Za-z0-9:_-]{0,120}"))throw new IllegalArgumentException("Invalid cursor");
                JSONArray page=store.page(after);data=new JSONObject().put("kind",Target.KIND).put("entries",page).put("after",page.length()==0?after:page.getJSONObject(page.length()-1).getString("id"));
            }
            else throw new IllegalArgumentException("Unknown operation");
            result.putString("data64",android.util.Base64.encodeToString(data.toString().getBytes(StandardCharsets.UTF_8),android.util.Base64.NO_WRAP));return result;
        }catch(JSONException e){throw new IllegalArgumentException("Invalid record");}}
        @Override public Cursor query(Uri u,String[] p,String s,String[] a,String o){authorize();throw new UnsupportedOperationException();}
        @Override public String getType(Uri u){authorize();return "application/json";}
        @Override public Uri insert(Uri u,ContentValues v){authorize();throw new UnsupportedOperationException();}
        @Override public int delete(Uri u,String s,String[] a){authorize();throw new UnsupportedOperationException();}
        @Override public int update(Uri u,ContentValues v,String s,String[] a){authorize();throw new UnsupportedOperationException();}
    }
}
