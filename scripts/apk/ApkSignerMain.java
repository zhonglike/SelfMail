import com.android.apksig.ApkSigner;
import java.io.File;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.List;

/** 用 apksig 对 APK 做 v1+v2+v3 签名。args: in.apk out.apk keystore password */
public class Signer {
    public static void main(String[] args) throws Exception {
        KeyStore ks = KeyStore.getInstance("PKCS12");
        ks.load(new java.io.FileInputStream(args[2]), args[3].toCharArray());
        String alias = ks.aliases().nextElement();
        PrivateKey pk = (PrivateKey) ks.getKey(alias, args[3].toCharArray());
        List<X509Certificate> certs = new ArrayList<X509Certificate>();
        for (Certificate cert : ks.getCertificateChain(alias)) {
            certs.add((X509Certificate) cert);
        }
        ApkSigner.SignerConfig sc =
                new ApkSigner.SignerConfig.Builder("SelfMail", pk, certs).build();
        List<ApkSigner.SignerConfig> signers = new ArrayList<ApkSigner.SignerConfig>();
        signers.add(sc);
        new ApkSigner.Builder(signers)
                .setInputApk(new File(args[0]))
                .setOutputApk(new File(args[1]))
                .setMinSdkVersion(24)
                .setV1SigningEnabled(true)
                .setV2SigningEnabled(true)
                .setV3SigningEnabled(true)
                .build()
                .sign();
        System.out.println("signed: " + args[1]);
    }
}
