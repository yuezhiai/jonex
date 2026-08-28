/**
 * 上传支持的文件格式常量
 * 用于 accept 属性、表单默认值，以及 UI 展示
 */

/** accept 属性 / 逗号分隔的扩展名（小写） */
export const ACCEPT_EXTENSIONS =
  '.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.md,.jpg,.jpeg,.png,.gif,.bmp,.tiff,.tif,.webp,.mp3,.wav,.flac,.aac,.m4a,.ogg,.wma,.opus,.amr,.mp4,.avi,.mov,.mkv,.flv,.wmv,.webm,.m4v,.mpg,.mpeg,.3gp';

/** 无前缀逗号分隔的扩展名（用于表单输入 placeholder 等场景） */
export const EXTENSIONS_NO_DOT =
  'pdf,doc,docx,ppt,pptx,xls,xlsx,txt,md,jpg,jpeg,png,gif,bmp,tiff,tif,webp,mp3,wav,flac,aac,m4a,ogg,wma,opus,amr,mp4,avi,mov,mkv,flv,wmv,webm,m4v,mpg,mpeg,3gp';

/** UI 展示用格式字符串（大写 · 分隔） */
export const FORMAT_DISPLAY =
  'PDF · DOC · DOCX · PPT · PPTX · XLS · XLSX · TXT · MD · JPG · JPEG · PNG · GIF · BMP · TIFF · TIF · WEBP · MP3 · WAV · FLAC · AAC · M4A · OGG · WMA · OPUS · AMR · MP4 · AVI · MOV · MKV · FLV · WMV · WEBM · M4V · MPG · MPEG · 3GP';

/** 批量上传：并发个数上限（配合字节预算调度，见 utils/batchUpload.ts） */
export const BATCH_UPLOAD_MAX_PARALLEL = 3;

/** 批量上传：在途字节预算上限 500MB（约束 Gateway 峰值内存增量） */
export const BATCH_UPLOAD_MAX_INFLIGHT_BYTES = 500 * 1024 * 1024;
