// Build-time hook for JOAL 2.1.37. Record only the hostname of the URI actually
// returned to TrackerClient, before a failure can advance the provider.
import java.nio.file.*;
import jdk.internal.org.objectweb.asm.*;

public class RecordTrackerHost {
    public static void main(String[] args) throws Exception {
        Path file = Path.of(args[0]);
        ClassReader input = new ClassReader(Files.readAllBytes(file));
        ClassWriter output = new ClassWriter(input, ClassWriter.COMPUTE_MAXS);
        int[] hooks = {0};
        input.accept(new ClassVisitor(Opcodes.ASM7, output) {
            @Override public MethodVisitor visitMethod(int flags, String name, String desc, String sig, String[] ex) {
                MethodVisitor method = super.visitMethod(flags, name, desc, sig, ex);
                if (!name.equals("get") || !desc.equals("()Ljava/net/URI;")) return method;
                return new MethodVisitor(Opcodes.ASM7, method) {
                    @Override public void visitInsn(int op) {
                        if (op == Opcodes.ARETURN) {
                            super.visitInsn(Opcodes.DUP);
                            super.visitVarInsn(Opcodes.ALOAD, 0);
                            super.visitMethodInsn(Opcodes.INVOKESTATIC, "org/araymond/joal/telemetry/UploadTelemetry",
                                "recordHost", "(Ljava/net/URI;Ljava/lang/Object;)V", false);
                            hooks[0]++;
                        }
                        super.visitInsn(op);
                    }
                };
            }
        }, 0);
        if (hooks[0] == 0) throw new IllegalStateException("TrackerClientUriProvider.get incompatible");
        Files.write(file, output.toByteArray());
    }
}
