#include "native_agent_music.c"
extern int pipe(int *);

int main(int argc, char **argv) {
    if (argc != 5) return 10;
    struct agent a = {0};
    int fds[2]; if (pipe(fds)) return 11;
    a.fd=fds[1]; a.chat=argv[1][0]=='1'; a.native_state=-1;
    a.focus_current=a.focus_pending=-1;
    unsigned char frame[4096], request[3072];
    size_t size=0, req=0;
    frame[size++]=8; frame[size++]=(unsigned char)(argv[2][0]-'0');
    const char id[]="12345678-1234-4123-8123-123456789abc";
    size+=put_bytes(frame+size,7,id,36);
    req+=put_bytes(request+req,1,argv[4],strlen(argv[4]));
    request[req++]=16; request[req++]=(unsigned char)(argv[3][0]-'0');
    request[req++]=32; request[req++]=1;
    size+=put_bytes(frame+size,15,request,req);
    if (!handle_state(&a,frame,size)) return 12;
    if (!handle_state(&a,frame,size)) return 13;
    char summary[200];
    int n=snprintf(summary,sizeof(summary),"{\"commands\":%u,\"clears\":%u,\"chat\":%d,\"seen\":%u}\n",a.commands,a.queue_clear_requests,a.chat,a.seen_count);
    write(1,summary,(size_t)n);
    close(fds[0]);close(fds[1]);return 0;
}
