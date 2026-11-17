package mylib.config

/**
 * Installer settings for installation configuration.
 */
class InstallerSettings implements Serializable {

    String installerType
    String versionVarFile
    List<String> installerVarFiles = []
    Map installerVars = [:]
    List<String> featureFlagsVarFiles = []
    boolean runInstallIdempotence = false
    String operatorChannel = ''
    List<String> operatorInstallVarFiles = []
    List<String> platformVarFiles = []
    boolean disableOptionalFeatures = true
    boolean gatewayTlsEnabled = false

    static InstallerSettings fromMap(Map params) {
        def settings = new InstallerSettings()
        if (!(params.installer instanceof Map)) {
            settings.installerType = 'traditional'
            return settings
        }
        def i = params.installer as Map
        settings.installerType = i.type ?: i.installerType ?: 'traditional'
        settings.versionVarFile = i.versionVarFile ?: i.versionFile
        settings.installerVarFiles = (i.varFiles ?: i.installerVarFiles ?: []) as List<String>
        return settings
    }

    Map toMap() {
        def excludedFields = ['class', 'metaClass'] as Set
        return this.properties.findAll { key, value ->
            !excludedFields.contains(key) && value != null
        }
    }

}
